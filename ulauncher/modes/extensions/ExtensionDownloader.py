import os
import logging
from functools import lru_cache
from urllib.request import urlretrieve
from tempfile import mktemp
from shutil import rmtree
from datetime import datetime
from typing import Tuple

from ulauncher.config import API_VERSION, PATHS
from ulauncher.utils.untar import untar
from ulauncher.utils.version import satisfies
from ulauncher.modes.extensions.ExtensionDb import ExtensionDb, ExtensionRecord
from ulauncher.modes.extensions.ExtensionRemote import ExtensionRemote
from ulauncher.modes.extensions.ExtensionManifest import ExtensionManifest, ExtensionIncompatibleWarning

logger = logging.getLogger()


class ExtensionAlreadyInstalledWarning(Exception):
    pass


class ExtensionDownloaderError(Exception):
    pass


class ExtensionDownloader:
    @classmethod
    @lru_cache(maxsize=None)
    def get_instance(cls) -> "ExtensionDownloader":
        ext_db = ExtensionDb.load()
        return cls(ext_db)

    def __init__(self, ext_db: ExtensionDb):
        super().__init__()
        self.ext_db = ext_db

    def download(self, url: str) -> str:
        """
        1. check if ext already exists
        2. get last commit info
        3. download & untar
        4. add it to the db

        :rtype: str
        :returns: Extension ID
        :raises AlreadyDownloadedError:
        """
        remote = ExtensionRemote(url)

        # 1. check if ext already exists
        ext_path = os.path.join(PATHS.EXTENSIONS, remote.extension_id)
        # allow user to re-download an extension if it's not running
        # most likely it has some problems with manifest file if it's not running
        if os.path.exists(ext_path):
            raise ExtensionAlreadyInstalledWarning(f'Extension with URL "{url}" is already installed')

        # 2. get last commit info
        commit_hash = remote.get_compatible_hash()

        # 3. download & untar
        filename = download_tarball(remote.get_download_url(commit_hash))
        untar(filename, ext_path, strip=1)

        manifest = ExtensionManifest.load_from_extension_id(remote.extension_id)
        if not satisfies(API_VERSION, manifest.api_version):
            if not satisfies("2.0", manifest.api_version):
                rmtree(ext_path)
                raise ExtensionIncompatibleWarning(f"{manifest.name} does not support Ulauncher API v{API_VERSION}.")
            logger.warning("Falling back on using API 2.0 version for %s.", remote.url)

        # 4. add to the db
        self.ext_db.save(
            {
                remote.extension_id: {
                    "id": remote.extension_id,
                    "url": url,
                    "updated_at": datetime.now().isoformat(),
                    "last_commit": commit_hash,
                }
            }
        )

        return remote.extension_id

    def remove(self, ext_id: str) -> None:
        rmtree(os.path.join(PATHS.EXTENSIONS, ext_id))
        if ext_id in self.ext_db:
            del self.ext_db[ext_id]
            self.ext_db.save()

    def update(self, ext_id: str) -> bool:
        """
        :raises ExtensionDownloaderError:
        :rtype: boolean
        :returns: False if already up-to-date, True if was updated
        """
        has_update, commit_hash = self.check_update(ext_id)
        if not has_update:
            return False
        ext = self._find_extension(ext_id)

        logger.info('Updating extension "%s" from commit %s to %s', ext_id, ext.last_commit[:8], commit_hash[:8])

        url = ExtensionRemote(ext.url).get_download_url(commit_hash)
        filename = download_tarball(url)
        tmpdir = f"{PATHS.EXTENSIONS}/{ext_id}_tmp"
        untar(filename, tmpdir, strip=1)

        manifest = ExtensionManifest.load_from_extension_id(ext_id)
        if not satisfies(API_VERSION, manifest.api_version):
            if not satisfies("2.0", manifest.api_version):
                rmtree(tmpdir)
                raise ExtensionIncompatibleWarning(f"{manifest.name} does not support Ulauncher API v{API_VERSION}.")
            logger.warning("Falling back on using API 2.0 version for %s.", ext_id)

        ext.update(
            updated_at=datetime.now().isoformat(),
            last_commit=commit_hash,
        )

        self.ext_db.save({ext_id: ext})

        return True

    def check_update(self, ext_id: str) -> Tuple[bool, str]:
        """
        Returns tuple with commit info about a new version
        """
        ext = self._find_extension(ext_id)
        commit_hash = ExtensionRemote(ext.url).get_compatible_hash()
        has_update = ext.last_commit != commit_hash
        return has_update, commit_hash

    def _find_extension(self, ext_id: str) -> ExtensionRecord:
        ext = self.ext_db.get(ext_id)
        if not ext:
            raise ExtensionDownloaderError("Extension not found")
        return ext


def download_tarball(url: str) -> str:
    dest_tar = mktemp(".tar.gz", prefix="ulauncher_dl_")
    filename, _ = urlretrieve(url, dest_tar)

    return filename
