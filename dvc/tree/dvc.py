import logging
import os

from dvc.exceptions import OutputNotFoundError
from dvc.path_info import PathInfo

from .base import BaseTree, RemoteActionNotImplemented

logger = logging.getLogger(__name__)


class DvcTree(BaseTree):  # pylint:disable=abstract-method
    """DVC repo tree.

    Args:
        repo: DVC repo.
        fetch: if True, uncached DVC outs will be fetched on `open()`.
        stream: if True, uncached DVC outs will be streamed directly from
            remote on `open()`.

    `stream` takes precedence over `fetch`. If `stream` is enabled and
    a remote does not support streaming, uncached DVC outs will be fetched
    as a fallback.
    """

    def __init__(self, repo, fetch=False, stream=False):
        super().__init__(repo, {"url": repo.root_dir})
        self.fetch = fetch
        self.stream = stream

    def _find_outs(self, path, *args, **kwargs):
        outs = self.repo.find_outs_by_path(path, *args, **kwargs)

        def _is_cached(out):
            return out.use_cache

        outs = list(filter(_is_cached, outs))
        if not outs:
            raise OutputNotFoundError(path, self.repo)

        return outs

    def _get_granular_checksum(self, path, out, remote=None):
        assert isinstance(path, PathInfo)
        if not self.fetch and not self.stream:
            raise FileNotFoundError
        dir_cache = out.get_dir_cache(remote=remote)
        for entry in dir_cache:
            entry_relpath = entry[out.tree.PARAM_RELPATH]
            if os.name == "nt":
                entry_relpath = entry_relpath.replace("/", os.sep)
            if path == out.path_info / entry_relpath:
                return entry[out.tree.PARAM_CHECKSUM]
        raise FileNotFoundError

    def open(
        self, path, mode="r", encoding="utf-8", remote=None
    ):  # pylint: disable=arguments-differ
        try:
            outs = self._find_outs(path, strict=False)
        except OutputNotFoundError as exc:
            raise FileNotFoundError from exc

        # NOTE: this handles both dirty and checkout-ed out at the same time
        if self.repo.tree.exists(path):
            return self.repo.tree.open(path, mode=mode, encoding=encoding)

        if len(outs) != 1 or (
            outs[0].is_dir_checksum and path == outs[0].path_info
        ):
            raise IsADirectoryError

        out = outs[0]
        if out.changed_cache(filter_info=path):
            if not self.fetch and not self.stream:
                raise FileNotFoundError

            remote_obj = self.repo.cloud.get_remote(remote)
            if self.stream:
                if out.is_dir_checksum:
                    checksum = self._get_granular_checksum(path, out)
                else:
                    checksum = out.checksum
                try:
                    remote_info = remote_obj.tree.hash_to_path_info(checksum)
                    return remote_obj.tree.open(
                        remote_info, mode=mode, encoding=encoding
                    )
                except RemoteActionNotImplemented:
                    pass
            cache_info = out.get_used_cache(filter_info=path, remote=remote)
            self.repo.cloud.pull(cache_info, remote=remote)

        if out.is_dir_checksum:
            checksum = self._get_granular_checksum(path, out)
            cache_path = out.cache.tree.hash_to_path_info(checksum).url
        else:
            cache_path = out.cache_path
        return open(cache_path, mode=mode, encoding=encoding)

    def exists(self, path):  # pylint: disable=arguments-differ
        try:
            self._find_outs(path, strict=False, recursive=True)
            return True
        except OutputNotFoundError:
            return False

    def isdir(self, path):  # pylint: disable=arguments-differ
        if not self.exists(path):
            return False

        path_info = PathInfo(os.path.abspath(path))
        outs = self._find_outs(path, strict=False, recursive=True)
        if len(outs) != 1:
            return True

        out = outs[0]
        if not out.is_dir_checksum:
            return out.path_info != path_info
        if out.path_info == path_info:
            return True

        # for dir checksum, we need to check if this is a file inside the
        # directory
        try:
            self._get_granular_checksum(path_info, out)
            return False
        except FileNotFoundError:
            return True

    def isfile(self, path):  # pylint: disable=arguments-differ
        if not self.exists(path):
            return False

        return not self.isdir(path)

    def _add_dir(self, top, trie, out, download_callback=None, **kwargs):
        if not self.fetch and not self.stream:
            return

        # pull dir cache if needed
        dir_cache = out.get_dir_cache(**kwargs)

        # pull dir contents if needed
        if self.fetch and out.changed_cache(filter_info=top):
            used_cache = out.get_used_cache(filter_info=top)
            downloaded = self.repo.cloud.pull(used_cache, **kwargs)
            if download_callback:
                download_callback(downloaded)

        for entry in dir_cache:
            entry_relpath = entry[out.tree.PARAM_RELPATH]
            if os.name == "nt":
                entry_relpath = entry_relpath.replace("/", os.sep)
            path_info = out.path_info / entry_relpath
            trie[path_info.parts] = None

    def _walk(self, root, trie, topdown=True, **kwargs):
        dirs = set()
        files = []

        out = trie.get(root.parts)
        if out and out.is_dir_checksum:
            self._add_dir(root, trie, out, **kwargs)

        root_len = len(root.parts)
        for key, out in trie.iteritems(prefix=root.parts):  # noqa: B301
            if key == root.parts:
                continue

            name = key[root_len]
            if len(key) > root_len + 1 or (out and out.is_dir_checksum):
                dirs.add(name)
                continue

            files.append(name)

        assert topdown
        dirs = list(dirs)
        yield root.fspath, dirs, files

        for dname in dirs:
            yield from self._walk(root / dname, trie)

    def walk(self, top, topdown=True, onerror=None, **kwargs):
        from pygtrie import Trie

        assert topdown

        if not self.exists(top):
            if onerror is not None:
                onerror(FileNotFoundError(top))
            return

        if not self.isdir(top):
            if onerror is not None:
                onerror(NotADirectoryError(top))
            return

        root = PathInfo(os.path.abspath(top))
        outs = self._find_outs(top, recursive=True, strict=False)

        trie = Trie()

        for out in outs:
            trie[out.path_info.parts] = out

            if out.is_dir_checksum and root.isin_or_eq(out.path_info):
                self._add_dir(top, trie, out, **kwargs)

        yield from self._walk(root, trie, topdown=topdown, **kwargs)

    def isdvc(self, path, **kwargs):
        try:
            return len(self._find_outs(path, **kwargs)) == 1
        except OutputNotFoundError:
            pass
        return False

    def isexec(self, path):  # pylint: disable=unused-argument
        return False

    def get_file_hash(self, path_info):
        outs = self._find_outs(path_info, strict=False)
        if len(outs) != 1:
            raise OutputNotFoundError
        out = outs[0]
        if out.is_dir_checksum:
            return (
                out.tree.PARAM_CHECKSUM,
                self._get_granular_checksum(path_info, out),
            )
        return out.tree.PARAM_CHECKSUM, out.checksum