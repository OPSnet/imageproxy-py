import asyncio
import datetime
import hmac
import logging
from os.path import join as path_join
from base64 import urlsafe_b64encode, urlsafe_b64decode
from hashlib import sha224, sha256

from aiofiles.os import path
from blacksheep import Application, Request, Response, bad_request
from blacksheep.server import responses

from downloader import ImgproxyDownloader, DownloadInProgress, BadResponse


logger = logging.getLogger('uvicorn.application')


IMGPROXY_URL = 'http://127.0.0.1:8090/'
SIG_SECRET = b'1234'
SIG_LENGTH_BYTES = 12
ALLOWED_SIZES = {b'150', b'250', b'500'}
DOWNLOAD_TIMEOUT_SEC = 30


app = Application()


class BadOptions(Exception):
    pass


class SignatureValidator:
    SIG_LENGTH_BASE64 = int(SIG_LENGTH_BYTES * 1.34)
    WEEK_DELTA = datetime.timedelta(weeks=1)

    def __init__(self, key: bytes):
        self.key = key

    def _get_now(self) -> datetime.datetime:
        return datetime.datetime.now()

    def _calc_sig(self, iso_tuple: tuple[int, int, int], msg: bytes) -> bytes:
        key = self.key + "{:04}{:02}".format(iso_tuple.year, iso_tuple.week).encode('ascii')
        return urlsafe_b64encode(hmac.HMAC(key, msg, digestmod=sha256)
                                 .digest()[:SIG_LENGTH_BYTES])

    def verify_signature(self, msg: bytes, sig: bytes) -> bool:
        if len(sig) != self.SIG_LENGTH_BASE64:
            return False

        now = self._get_now()
        iso_week = now.isocalendar()
        # check for current week
        real_sig = self._calc_sig(iso_week, msg)
        if sig == real_sig:
            return True

        # check for last week
        last_week = now - self.WEEK_DELTA
        last_week_iso = last_week.isocalendar()
        real_sig = self._calc_sig(last_week_iso, msg)
        return sig == real_sig


def parse_imgproxy_params(params: str) -> list[bytes]:
    if params == 'full':
        return []
    params = params.encode('ascii')
    sizes = params.split(b'x')
    if not sizes:
        raise BadOptions()
    opts = []
    try:
        height, width = sizes[0], sizes[1]
    except:
        raise BadOptions()

    if width and width in ALLOWED_SIZES:
        opts.append(b'w:' + width)

    if height and height in ALLOWED_SIZES:
        opts.append(b'h:' + height)

    if not opts:
        raise BadOptions()

    return opts


def create_filename(bucket: bytes, img_url: bytes) -> bytes:
    img_url_hash = sha224(img_url).digest()
    hash_b64 = urlsafe_b64encode(img_url_hash)
    return bucket + b'/' + hash_b64[0:1] + b'/' + hash_b64[1:2] + b'/' + hash_b64


requester = ImgproxyDownloader(IMGPROXY_URL)
#requester = ImgproxyDownloader('')
#requester._get_full_path = lambda x: urlsafe_b64decode(x).decode('ascii')


async def download_file(img_url: str, file_path: str, requester=requester):
    try:
        return await requester.save(img_url, file_path)
    except DownloadInProgress:
        for i in range(DOWNLOAD_TIMEOUT_SEC):
            await asyncio.sleep(1)
            if await path.isfile(file_path):
                return await requester.read_headers(file_path)
        raise BadResponse('stalled download')


validator = SignatureValidator(SIG_SECRET)


@app.router.get("/*")
async def handle_signed_request(request: Request) -> Response:
    # check path
    url = request.route_values['tail'].split('/')
    if len(url) not in (4, 5):
        logger.debug("wrong number of path elements: %d", len(url))
        return bad_request()

    bucket = url.pop(0).encode('ascii')

    img_url = url[2].encode('ascii')
    sig_msg = bucket + b'/' + img_url
    if len(url) == 5:
        sig_msg += b'/' + url[3].encode('ascii')

    # validate signature
    if not validator.verify_signature(sig_msg, url[1].encode('ascii')):
        logger.debug("failed to verify signature %s for %s", url[1], sig_msg)
        return responses.status_code(410)

    if logger.isEnabledFor(logging.INFO):
        url_b64 = url[2] + ("=" * (len(url[2]) % 4))
        log_url = urlsafe_b64decode(url_b64).decode('ascii', errors='ignore')
        if len(log_url) > 100 and not logger.isEnabledFor(logging.DEBUG):
            log_url = log_url[:95] + '<...>'
    else:
        log_url = url[2]

    # parse options
    is_proxy = len(url) == 4 and url[3] == 'proxy'

    try:
        imgproxy_opts = [] if is_proxy else parse_imgproxy_params(url[0])
    except BadOptions:
        logger.debug("invalid resize options: %s", url[0])
        return bad_request()

    is_resize = bool(imgproxy_opts)

    img_path = create_filename(bucket, img_url)
    imgproxy_opts.append(b'fn:' + img_url)

    # download and perma-store image through imgproxy
    base_path = request.get_first_header(b'root_dir').decode('ascii')
    file_path = path_join(base_path, img_path.decode('ascii'))
    if not is_proxy and not await path.isfile(file_path):
        try:
            headers = await download_file(url[2], file_path)
        except BadResponse as e:
            err_msg = "failed to fetch from remote server"
            if e.msg:
                err_msg += f" ({e.msg})"

            if e.msg.startswith('backoff'):
                logger.debug("failed to fetch %s: %s", log_url, e.msg)
            else:
                logger.info("failed to fetch %s: %s", log_url, e.msg)
            return responses.status_code(520, err_msg)
        logger.info("downloaded image %s", log_url)
    else:
        headers = await requester.read_headers(file_path)

    # set headers and leave everything else to nginx
    if is_proxy:
        resp = responses.status_code(305)
    elif is_resize:
        resp = responses.see_other(b'/' + img_path)
    else:
        resp = responses.redirect(b'/' + img_path)
    resp.set_header(b'imgproxy-opts', b'/'.join(imgproxy_opts) + b'/')
    resp.set_header(b'imgproxy-path', img_path)
    resp.set_header(b'imgproxy-url', img_url)
    for k, v in headers.items():
        resp.set_header(k.encode('ascii'), v.encode('ascii'))

    return resp
