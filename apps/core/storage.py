"""File storage in the database, for hosting without a persistent disk (Vercel / serverless).

Files are rows of `StoredFile`; they are served by `apps.core.views.media` to signed-in users only,
exactly like files on disk. Suitable for the modest volume of this system (product photos,
catalogues, quotation PDFs); a VPS installation keeps using the file system.
"""
import mimetypes
import posixpath

from django.core.files.base import ContentFile
from django.core.files.storage import Storage
from django.utils.deconstruct import deconstructible
from django.utils.encoding import filepath_to_uri


@deconstructible
class DatabaseStorage(Storage):
    def _model(self):
        from .models import StoredFile

        return StoredFile

    @staticmethod
    def _clean(name: str) -> str:
        name = posixpath.normpath(str(name).replace("\\", "/")).lstrip("/")
        if name.startswith("..") or name in ("", "."):
            raise ValueError(f"Invalid file name: {name!r}")
        return name

    def _open(self, name, mode="rb"):
        try:
            row = self._model().objects.only("content").get(name=self._clean(name))
        except self._model().DoesNotExist:
            raise FileNotFoundError(name) from None
        f = ContentFile(bytes(row.content), name=name)
        f.mode = mode
        return f

    def _save(self, name, content):
        name = self._clean(name)
        if hasattr(content, "seek"):
            content.seek(0)
        data = content.read()
        if isinstance(data, str):
            data = data.encode("utf-8")
        content_type = getattr(content, "content_type", None) or mimetypes.guess_type(name)[0] or "application/octet-stream"
        self._model().objects.update_or_create(
            name=name, defaults={"content": data, "size": len(data), "content_type": content_type[:100]})
        return name

    def delete(self, name):
        if name:
            self._model().objects.filter(name=self._clean(name)).delete()

    def exists(self, name):
        return self._model().objects.filter(name=self._clean(name)).exists()

    def size(self, name):
        row = self._model().objects.filter(name=self._clean(name)).only("size").first()
        if row is None:
            raise FileNotFoundError(name)
        return row.size

    def listdir(self, path):
        prefix = self._clean(path) + "/" if path not in ("", ".", "/") else ""
        dirs, files = set(), []
        for name in self._model().objects.filter(name__startswith=prefix).values_list("name", flat=True):
            rest = name[len(prefix):]
            if "/" in rest:
                dirs.add(rest.split("/", 1)[0])
            else:
                files.append(rest)
        return sorted(dirs), sorted(files)

    def url(self, name):
        from django.conf import settings

        return settings.MEDIA_URL + filepath_to_uri(self._clean(name))

    def get_modified_time(self, name):
        row = self._model().objects.filter(name=self._clean(name)).only("created_at").first()
        if row is None:
            raise FileNotFoundError(name)
        return row.created_at

    get_created_time = get_modified_time

    def path(self, name):
        raise NotImplementedError("Files are stored in the database; open them with storage.open().")
