import asyncio
import argparse
import aiofiles
from aiofiles.os import path
from base64 import urlsafe_b64encode
import tqdm
from os.path import join as path_join

from main import create_filename, requester


async def download(base_dir: str, url: str):
    img_url = urlsafe_b64encode(url.encode('ascii'))
    file_path = path_join(base_dir, create_filename(img_url).decode('ascii'))
    if await path.isfile(file_path):
        return
    await requester.save(img_url.decode('ascii'), file_path)


async def main():
    parser = argparse.ArgumentParser(description='Fetch images from text file')
    parser.add_argument('dest', help='base path to store images at')
    parser.add_argument('file', help='text file with links to import, one per line')
    parser.add_argument('--disable-parallel', '-x', action='store_true',
                        help='do not do multiple requests at the same time')
    args = parser.parse_args()

    if args.disable_parallel:
        worker_slots = 1
    else:
        worker_slots = 100
    sem = asyncio.Semaphore(worker_slots)
    success = []
    failed = []
    pbar = tqdm.tqdm(unit='req', postfix={'success': 0, 'failed': 0})

    def callback(task):
        try:
            task.result()
        except Exception:
            failed.append(task.get_name())
        else:
            success.append(task.get_name())
        finally:
            sem.release()
            pbar.update(1)
            pbar.set_postfix(success=len(success), failed=len(failed))

    i = 0
    async with aiofiles.open(args.file, 'r') as fh:
        async for line in fh:
            i += 1
            await sem.acquire()
            url = line.rstrip('\n')
            task = asyncio.create_task(download(args.dest, url))
            task.set_name(url)
            task.add_done_callback(callback)

    for x in range(worker_slots):
        # acquire all slots so no more tasks can be active
        await sem.acquire()

    pbar.close()

    async with aiofiles.open(args.file + '.success', 'w') as fh:
        await fh.writelines(x + '\n' for x in success)
    async with aiofiles.open(args.file + '.failed', 'w') as fh:
        await fh.writelines(x + '\n' for x in failed)

    print(f"all done. total {i}, success: {len(success)}, failed: {len(failed)}")


if __name__ == '__main__':
    asyncio.run(main())
