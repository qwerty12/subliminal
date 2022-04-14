# -*- coding: utf-8 -*-
import libcurl as lcurl
import logging
import re
import time

from babelfish import Language, language_converters
from ctypes import c_char, POINTER
from guessit import guessit
from requests import Timeout
from requests.utils import dict_from_cookiejar
from types import SimpleNamespace
from urllib.parse import urlencode

from . import ParserBeautifulSoup, Provider
from ..cache import SHOW_EXPIRATION_TIME, region
from ..curl import curl_write_function, curl_easy_impersonate, curl_raise_for_status, basic_resp_header_parser, curl_get_content_type
from ..exceptions import AuthenticationError, DownloadLimitExceeded, ProviderError, ServiceUnavailable
from ..matches import guess_matches
from ..subtitle import Subtitle, fix_line_ending
from ..utils import sanitize
from ..video import Episode

logger = logging.getLogger(__name__)

language_converters.register('addic7ed = subliminal.converters.addic7ed:Addic7edConverter')

#: Series header parsing regex
series_year_re = re.compile(r'^(?P<series>[ \w\'.:(),*&!?-]+?)(?: \((?P<year>\d{4})\))?$')


class Addic7edSubtitle(Subtitle):
    """Addic7ed Subtitle."""
    provider_name = 'addic7ed'

    def __init__(self, language, hearing_impaired, page_link, series, season, episode, title, year, version,
                 download_link):
        super(Addic7edSubtitle, self).__init__(language, hearing_impaired=hearing_impaired, page_link=page_link)
        self.series = series
        self.season = season
        self.episode = episode
        self.title = title
        self.year = year
        self.version = version
        self.download_link = download_link

    @property
    def id(self):
        return self.download_link

    @property
    def info(self):
        return '{series}{yopen}{year}{yclose} s{season:02d}e{episode:02d}{topen}{title}{tclose}{version}'.format(
            series=self.series, season=self.season, episode=self.episode, title=self.title, year=self.year or '',
            version=self.version, yopen=' (' if self.year else '', yclose=')' if self.year else '',
            topen=' - ' if self.title else '', tclose=' - ' if self.version else ''
        )

    def get_matches(self, video):
        # series name
        matches = guess_matches(video, {
            'title': self.series,
            'season': self.season,
            'episode': self.episode,
            'episode_title': self.title,
            'year': self.year,
            'release_group': self.version,
        })

        # resolution
        if video.resolution and self.version and video.resolution in self.version.lower():
            matches.add('resolution')
        # other properties
        if self.version:
            matches |= guess_matches(video, guessit(self.version, {'type': 'episode'}), partial=True)

        return matches


class Addic7edProvider(Provider):
    """Addic7ed Provider."""
    languages = {Language('por', 'BR')} | {Language(l) for l in [
        'ara', 'aze', 'ben', 'bos', 'bul', 'cat', 'ces', 'dan', 'deu', 'ell', 'eng', 'eus', 'fas', 'fin', 'fra', 'glg',
        'heb', 'hrv', 'hun', 'hye', 'ind', 'ita', 'jpn', 'kor', 'mkd', 'msa', 'nld', 'nor', 'pol', 'por', 'ron', 'rus',
        'slk', 'slv', 'spa', 'sqi', 'srp', 'swe', 'tha', 'tur', 'ukr', 'vie', 'zho'
    ]}
    video_types = (Episode,)
    domain = "www.addic7ed.com"
    server_url = f'https://{domain}/'
    subtitle_class = Addic7edSubtitle

    def __init__(self, phpsessid=None, fxcookies=False):
        self.phpsessid = phpsessid
        self.fxcookies = fxcookies
        self.cookie_string = ""
        self.session = None
        self.curl_reqheader_chunk = None
        self.curl_error_buffer = (c_char * lcurl.CURL_ERROR_SIZE)()
        self.curl_data_buffer = bytearray(b"")
        self.curl_header_buffer = bytearray(b"")
        self.last_request = 0

    def initialize(self):
        # login
        if self.phpsessid is not None:
            self.cookie_string = 'PHPSESSID=' + self.phpsessid
        elif self.fxcookies:
            logger.info('Using cookies from Firefox')
            from browser_cookie3 import firefox
            from ..utils import get_firefox_ua

            try:
                cookies = firefox(domain_name=self.domain)
                cookies = dict_from_cookiejar(cookies)
                # preserve Firefox order
                for wanted_cookie_name in ["wikisubtitlesuser", "wikisubtitlespass", "PHPSESSID"]:
                    self.cookie_string += f'{wanted_cookie_name}={cookies[wanted_cookie_name]}; '
            except Exception as e:
                raise AuthenticationError("Could not obtain Addic7ed cookies from Firefox: " + repr(e))

            try:
                self.user_agent = get_firefox_ua()
            except:
                logger.warn("Unable to determine Firefox's User-Agent: requests will be sent with Subliminal's UA!")

        self._curl_initialize()

    def _curl_initialize(self):
        self.session: POINTER(lcurl.CURL) = lcurl.easy_init()
        if not self.session:
            raise ProviderError("Unable to initialize cURL")
        lcurl.easy_setopt(self.session, lcurl.CURLOPT_ERRORBUFFER, self.curl_error_buffer)
        lcurl.easy_setopt(self.session, lcurl.CURLOPT_WRITEFUNCTION, curl_write_function)
        lcurl.easy_setopt(self.session, lcurl.CURLOPT_WRITEDATA, id(self.curl_data_buffer))
        lcurl.easy_setopt(self.session, lcurl.CURLOPT_FOLLOWLOCATION, 1)  # Mirror Requests - mostly
        #lcurl.easy_setopt(self.session, lcurl.CURLOPT_CONNECTTIMEOUT, 30)
        lcurl.easy_setopt(self.session, lcurl.CURLOPT_TIMEOUT, 30)
        if self.cookie_string and len(self.cookie_string) > 0:
            self.cookie_string = self.cookie_string.strip("; ") # Fx does not send a cookie string with a terminating semicolon
            lcurl.easy_setopt(self.session, lcurl.CURLOPT_COOKIE, self.cookie_string.encode("utf-8"))
        lcurl.easy_setopt(self.session, lcurl.CURLOPT_REFERER, self.server_url.encode("utf-8"))
        self.curl_reqheader_chunk = POINTER(lcurl.slist)()
        self.curl_reqheader_chunk = lcurl.slist_append(self.curl_reqheader_chunk, "Connection: keep-alive".encode("utf-8"))
        self.curl_reqheader_chunk = lcurl.slist_append(self.curl_reqheader_chunk, "User-Agent: {0}".format(self.user_agent).encode("utf-8"))
        lcurl.easy_setopt(self.session, lcurl.CURLOPT_HTTPHEADER, self.curl_reqheader_chunk)
        lcurl.easy_setopt(self.session, lcurl.CURLOPT_ACCEPT_ENCODING, "gzip, deflate, br".encode("utf-8")) # enable cURL automatic decompression...
        curl_easy_impersonate(self.session, "ff98") #... even though this sets Accept-Encoding

    def _curl_make_request(self, url, params=None, store_resp_headers=False):
        if time.time() < self.last_request + 5:
            time.sleep(5)
        self.last_request = time.time()

        if params is not None:
            if not url.endswith("?"):
                url += "?"
            url += urlencode(params, doseq=True)

        lcurl.easy_setopt(self.session, lcurl.CURLOPT_URL, url.encode("utf-8"))
        if not store_resp_headers:
            lcurl.easy_setopt(self.session, lcurl.CURLOPT_HEADERFUNCTION, None)
            lcurl.easy_setopt(self.session, lcurl.CURLOPT_HEADERDATA, None)
        else:
            lcurl.easy_setopt(self.session, lcurl.CURLOPT_HEADERFUNCTION, curl_write_function)
            lcurl.easy_setopt(self.session, lcurl.CURLOPT_HEADERDATA, id(self.curl_header_buffer))
            self.curl_header_buffer.clear()

        self.curl_data_buffer.clear()
        res: lcurl.CURLcode = lcurl.easy_perform(self.session)
        if res != lcurl.CURLE_OK:
            logger.error("Failed to get '%s': [%d] %s", url, res, self.curl_error_buffer.raw.decode("utf-8"))
            if res == lcurl.CURLE_OPERATION_TIMEDOUT:
                raise Timeout()

        if not store_resp_headers:
            details = None
            curl_raise_for_status(self.session, url)
        else:
            details = basic_resp_header_parser(bytes(self.curl_header_buffer))
            curl_raise_for_status(self.session, url, details.request_line)

        return SimpleNamespace(content=bytes(self.curl_data_buffer), details=details)

    def terminate(self):
        if self.curl_reqheader_chunk is not None:
            lcurl.slist_free_all(self.curl_reqheader_chunk)
            self.curl_reqheader_chunk = None

        if self.session is not None:
            lcurl.easy_cleanup(self.session)
            self.session = None

    @region.cache_on_arguments(expiration_time=SHOW_EXPIRATION_TIME)
    def _get_show_ids(self):
        """Get the ``dict`` of show ids per series by querying the `shows.php` page.

        :return: show id per series, lower case and without quotes.
        :rtype: dict

        """
        # get the show page
        logger.info('Getting show ids')
        r = self._curl_make_request(self.server_url + 'shows.php')

        soup = ParserBeautifulSoup(r.content, ['html.parser'])

        # populate the show ids
        show_ids = {}
        for show in soup.select('td.vr > h3 > a[href^="/show/"]'):
            show_ids[sanitize(show.text)] = int(show['href'][6:])
        logger.debug('Found %d show ids', len(show_ids))

        return show_ids

    @region.cache_on_arguments(expiration_time=SHOW_EXPIRATION_TIME)
    def _search_show_id(self, series, year=None):
        """Search the show id from the `series` and `year`.

        :param str series: series of the episode.
        :param year: year of the series, if any.
        :type year: int
        :return: the show id, if found.
        :rtype: int

        """
        # addic7ed doesn't support search with quotes
        series = series.replace('\'', ' ')

        # build the params
        series_year = '%s %d' % (series, year) if year is not None else series
        params = {'search': series_year, 'Submit': 'Search'}

        # make the search
        logger.info('Searching show ids with %r', params)
        r = self._curl_make_request(self.server_url + 'srch.php', params)
        soup = ParserBeautifulSoup(r.content, ['lxml', 'html.parser'])

        # get the suggestion
        suggestion = soup.select('span.titulo > a[href^="/show/"]')
        if not suggestion:
            logger.warning('Show id not found: no suggestion')
            return None
        if not sanitize(suggestion[0].i.text.replace('\'', ' ')) == sanitize(series_year):
            logger.warning('Show id not found: suggestion does not match')
            return None
        show_id = int(suggestion[0]['href'][6:])
        logger.debug('Found show id %d', show_id)

        return show_id

    def get_show_id(self, series, year=None, country_code=None):
        """Get the best matching show id for `series`, `year` and `country_code`.

        First search in the result of :meth:`_get_show_ids` and fallback on a search with :meth:`_search_show_id`.

        :param str series: series of the episode.
        :param year: year of the series, if any.
        :type year: int
        :param country_code: country code of the series, if any.
        :type country_code: str
        :return: the show id, if found.
        :rtype: int

        """
        series_sanitized = sanitize(series).lower()
        show_ids = self._get_show_ids()
        show_id = None

        # attempt with country
        if not show_id and country_code:
            logger.debug('Getting show id with country')
            show_id = show_ids.get('%s %s' % (series_sanitized, country_code.lower()))

        # attempt with year
        if not show_id and year:
            logger.debug('Getting show id with year')
            show_id = show_ids.get('%s %d' % (series_sanitized, year))

        # attempt clean
        if not show_id:
            logger.debug('Getting show id')
            show_id = show_ids.get(series_sanitized)

        # search as last resort
        if not show_id:
            logger.warning('Series %s not found in show ids', series)
            show_id = self._search_show_id(series)

        return show_id

    def query(self, show_id, series, season, year=None, country=None):
        # get the page of the season of the show
        logger.info('Getting the page of show id %d, season %d', show_id, season)
        r = self._curl_make_request(self.server_url + 'show/%d' % show_id, {'season': season})

        if not r.content or len(r.content) == 0:
            # Provider returns a status of 304 Not Modified with an empty content
            # raise_for_status won't raise exception for that status code
            logger.debug('No data returned from provider')
            return []

        err = b"Server too busy. Please try again later."
        if r.content.endswith(err):
            raise ServiceUnavailable(err)

        soup = ParserBeautifulSoup(r.content, ['lxml', 'html.parser'])

        # loop over subtitle rows
        match = series_year_re.match(soup.select('#header font')[0].text.strip()[:-10])
        series = match.group('series')
        year = int(match.group('year')) if match.group('year') else None
        subtitles = []
        for row in soup.select('tr.epeven'):
            cells = row('td')

            # ignore incomplete subtitles
            status = cells[5].text
            if status != 'Completed':
                logger.debug('Ignoring subtitle with status %s', status)
                continue

            # read the item
            language = Language.fromaddic7ed(cells[3].text)
            hearing_impaired = bool(cells[6].text)
            page_link = self.server_url + cells[2].a['href'][1:]
            season = int(cells[0].text)
            episode = int(cells[1].text)
            title = cells[2].text
            version = cells[4].text
            download_link = cells[9].a['href'][1:]

            subtitle = self.subtitle_class(language, hearing_impaired, page_link, series, season, episode, title, year,
                                           version, download_link)
            logger.debug('Found subtitle %r', subtitle)
            subtitles.append(subtitle)

        return subtitles

    def list_subtitles(self, video, languages):
        # lookup show_id
        titles = [video.series] + video.alternative_series
        show_id = None
        for title in titles:
            show_id = self.get_show_id(title, video.year)
            if show_id is not None:
                break

        # query for subtitles with the show_id
        if show_id is not None:
            subtitles = [s for s in self.query(show_id, title, video.season, video.year)
                         if s.language in languages and s.episode == video.episode]
            if subtitles:
                return subtitles
        else:
            logger.error('No show id found for %r (%r)', video.series, {'year': video.year})

        return []

    def download_subtitle(self, subtitle):
        # download the subtitle
        logger.info('Downloading subtitle %r', subtitle)
        r = self._curl_make_request(self.server_url + subtitle.download_link)

        if not r.content or len(r.content) == 0:
            # Provider returns a status of 304 Not Modified with an empty content
            # raise_for_status won't raise exception for that status code
            logger.debug('Unable to download subtitle. No data returned from provider')
            return

        # detect download limit exceeded
        if curl_get_content_type(self.session).startswith('text/html'):
            raise DownloadLimitExceeded

        subtitle.content = fix_line_ending(r.content)

