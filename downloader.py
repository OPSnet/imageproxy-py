from typing import Mapping
import httpx
import time
import logging
from aiofiles import os, open as aio_open


logger = logging.getLogger('uvicorn.application')


class BadResponse(Exception):
    def __init__(self, msg=None):
        self.msg = msg
        super().__init__()


class DownloadInProgress(Exception):
    pass


class ImgproxyDownloader:
    DEFAULT_OPTS = "max_bytes:2097152"  # 2MiB

    def __init__(self, base_url: str):
        self.base_url = base_url
        self.client = httpx.AsyncClient(base_url=base_url,
                                        follow_redirects=False)

    @staticmethod
    def _error_lock_name(file_path: str) -> str:
        return file_path + '.err'

    @staticmethod
    def _headers_name(file_path: str) -> str:
        return file_path + '.headers'

    @classmethod
    def _get_full_path(cls, img_url: str) -> str:
        return 'nosig/' + cls.DEFAULT_OPTS + '/' + img_url

    @classmethod
    async def read_headers(cls, file_path: str) -> Mapping[str, str]:
        headers = {}
        headers_path = cls._headers_name(file_path)
        async with aio_open(headers_path, mode='r') as fh:
            for line in await fh.readlines():
                k, v = line.rstrip('\n').split(' ', maxsplit=1)
                headers[k] = v
        return headers

    @classmethod
    async def _write_headers(cls, file_path: str, img_url: str,
                             headers: Mapping[str, str]) -> Mapping[str, str]:
        headers_path = cls._headers_name(file_path)
        save_headers = {'content-type': headers['content-type'],
                        'x-original-url': img_url}
        async with aio_open(headers_path, mode='w') as fh:
            for pair in save_headers.items():
                await fh.write(' '.join(pair) + '\n')

        return save_headers

    async def save(self, img_url: str, file_path: str) -> Mapping[str, str]:
        error_lock = ErrorLock(self._error_lock_name(file_path))
        has_error_lock = await error_lock.is_locked()
        if has_error_lock:
            logger.debug("found valid lock file, skipping download")
            raise BadResponse(f"backoff {error_lock.timeleft} sec")

        tmp_path = file_path + '.tmp'
        try:
            fp = await aio_open(tmp_path, mode='xb')
        except FileExistsError:
            raise DownloadInProgress()

        try:
            async with self.client.stream(
                    'GET', self._get_full_path(img_url)) as resp:
                if not resp or resp.status_code != 200:
                    raise BadResponse(f"bad http status: {resp.status_code}")

                async for chunk in resp.aiter_bytes():
                    await fp.write(chunk)

                headers = await self._write_headers(
                    file_path, img_url, resp.headers)
        except Exception as e:
            await fp.close()
            await error_lock.update()
            await os.remove(tmp_path)
            logger.info(f"failed to download: {e}")
            raise BadResponse(f"download error")
        else:
            await fp.close()
            await error_lock.remove()
            await os.rename(tmp_path, file_path)
            return headers


class ErrorLock:
    INITIAL_LOCK_DURATION = 60  # 1 min
    MAX_LOCK_DURATION = 60 * 60 * 24  # 1 day

    def __init__(self, path: str):
        self.path = path
        self.lock_time = None
        self.lock_duration = self.INITIAL_LOCK_DURATION
        self._now = self._get_now()
        self.exists = False

    @property
    def timeleft(self) -> int:
        return self.lock_time - self._now

    @staticmethod
    def _get_now() -> int:
        return int(time.time())

    async def is_locked(self) -> bool:
        try:
            async with aio_open(self.path, 'r') as fh:
                lock_time, lock_duration = (await fh.read()).split(' ')
            self.lock_time = int(lock_time)
            self.lock_duration = min(self.MAX_LOCK_DURATION, int(lock_duration) * 2)
            self.exists = True
            return self.lock_time > self._now
        except FileNotFoundError:
            self.exists = False
        except ValueError:
            # invalid lock file somehow
            logger.warning("found invalid lock file, removing")
            await self.remove()
        return False

    async def update(self) -> None:
        lock_until = (self.lock_time or self._now) + self.lock_duration
        async with aio_open(self.path, 'w') as fh:
            await fh.write('{} {}'.format(lock_until, self.lock_duration))

    async def remove(self) -> None:
        if self.exists:
            await os.remove(self.path)
            self.exists = False
