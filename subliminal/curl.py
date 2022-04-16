import os
import libcurl as lcurl
import ctypes as ct
from email.parser import BytesParser
from requests import HTTPError
from types import SimpleNamespace


def curl_init(dll_fullpath):
    # TODO: find a place to call lcurl.global_cleanup()
    if dll_fullpath is not None:
        # TODO: Not ideal for security reasons, but NSS won't load the rest of its modules without this
        dn = os.path.dirname(dll_fullpath)
        os.environ["PATH"] = dn + os.pathsep + os.environ["PATH"]
        if "SSL_CERT_FILE" in os.environ:
            if not os.path.isfile(os.path.join(dn, "nsspem.dll")):
                del os.environ["SSL_CERT_FILE"]
    lcurl.config(LIBCURL=dll_fullpath)
    lcurl.global_init(lcurl.CURL_GLOBAL_DEFAULT)


def curl_easy_impersonate(data, target):
    try:
        easy_impersonate = ct.WINFUNCTYPE(lcurl._curl.CURLcode,
                            ct.POINTER(lcurl._curl.CURL),
                            ct.c_char_p)(
                            ("curl_easy_impersonate", lcurl._dll.dll), (
                            (1, "data"),
                            (1, "target"),))
        return easy_impersonate(data, target.encode('utf-8'))
    except AttributeError:
        # Provided libcurl lacks impersonation support; try https://github.com/lwthiker/curl-impersonate
        return lcurl.CURLE_NOT_BUILT_IN


@lcurl.write_callback
def curl_write_function(buffer, size, nitems, stream):
    # Copyright (c) 2021-2022 Adam Karpierz
    # Licensed under the MIT License
    data_buffer = lcurl.from_oid(stream)
    buffer_size = size * nitems
    if buffer_size == 0: return 0
    data_buffer += bytes(buffer[:buffer_size])
    return buffer_size


def curl_get_resp_code(curl):
    response_code = ct.c_long()
    res = lcurl.easy_getinfo(curl, lcurl.CURLINFO_RESPONSE_CODE, ct.byref(response_code))
    if res == lcurl.CURLE_OK:
        return response_code.value
    return -1


def curl_get_content_type(curl):
    content_type = ct.c_char_p()
    res = lcurl.easy_getinfo(curl, lcurl.CURLINFO_CONTENT_TYPE, ct.byref(content_type))
    try:
        if res == lcurl.CURLE_OK and content_type:
            return content_type.value.decode("utf-8")
    except UnicodeDecodeError:
        pass
    return ""


def curl_raise_for_status(curl, url='', reason=None):
    # Copied from Requests
    """Raises :class:`HTTPError`, if one occurred."""

    status_code = curl_get_resp_code(curl)
    if not reason:
        reason = '<unknown>'

    http_error_msg = ''
    if 400 <= status_code < 500:
        http_error_msg = u'%s Client Error: %s for url: %s' % (status_code, reason, url)

    elif 500 <= status_code < 600:
        http_error_msg = u'%s Server Error: %s for url: %s' % (status_code, reason, url)

    if http_error_msg:
        raise HTTPError(http_error_msg, response=SimpleNamespace(status_code=status_code))


def basic_resp_header_parser(request_text):
    # Brandon Rhodes: https://stackoverflow.com/a/5955949
    request_line, headers_alone = request_text.split(b'\r\n', 1)
    headers = BytesParser().parsebytes(headers_alone)
    return SimpleNamespace(request_line=request_line, headers=headers, headers_raw=headers_alone)
