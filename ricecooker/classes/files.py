# Node models to represent channel's tree
from __future__ import unicode_literals

import os
import hashlib
import tempfile
import shutil
import youtube_dl
import requests
import zipfile
from subprocess import CalledProcessError
from le_utils.constants import content_kinds,file_formats, format_presets, exercises
from .. import config
from .nodes import ChannelNode, TopicNode, VideoNode, AudioNode, DocumentNode, ExerciseNode, HTML5AppNode
from ..exceptions import UnknownFileTypeError
from cachecontrol.caches.file_cache import FileCache
from pressurecooker.videos import extract_thumbnail_from_video, guess_video_preset_by_resolution, compress_video
from pressurecooker.encodings import get_base64_encoding, write_base64_to_file
from requests.exceptions import MissingSchema, HTTPError, ConnectionError, InvalidURL, InvalidSchema

# Cache for filenames
FILECACHE = FileCache(config.FILECACHE_DIRECTORY, forever=True)

def generate_key(action, path_or_id, settings=None, default=" (default)"):
    """ generate_key: generate key used for caching
        Args:
            action (str): how video is being processed (e.g. COMPRESSED or DOWNLOADED)
            path_or_id (str): path to video or youtube_id
            settings (dict): settings for compression or downloading passed in by user
            default (str): if settings are None, default to this extension (avoid overwriting keys)
        Returns: filename
    """
    settings = " {}".format(str(sorted(settings.items()))) if settings else default
    return "{}: {}{}".format(action.upper(), path_or_id, settings)

def download(path, default_ext=None):
    """ download: downloads file
        Args: None
        Returns: filename
    """
    key = "DOWNLOAD:{}".format(path)
    if not config.UPDATE and FILECACHE.get(key):
        return FILECACHE.get(key).decode('utf-8')

    config.LOGGER.info("\tDownloading {}".format(path))

    # Write file to temporary file
    with tempfile.TemporaryFile() as tempf:
        hash = write_and_get_hash(path, tempf)
        tempf.seek(0)

        # Get extension of file or default if none found
        extension = os.path.splitext(path)[1][1:].lower()
        if extension not in [key for key, value in file_formats.choices]:
            if default_ext:
                extension = default_ext
            else:
                raise IOError("No extension found: {}".format(path))

        filename = '{0}.{ext}'.format(hash.hexdigest(), ext=extension)

        copy_file_to_storage(filename, tempf)

        FILECACHE.set(key, bytes(filename, "utf-8"))

        return filename

def write_and_get_hash(path, write_to_file, hash=None):
    """ write_and_get_hash: write file
        Args: None
        Returns: Hash of file's contents
    """
    hash = hash or hashlib.md5()
    try:
        # Access path
        r = config.DOWNLOAD_SESSION.get(path, stream=True)
        r.raise_for_status()
        for chunk in r:
            write_to_file.write(chunk)
            hash.update(chunk)

    except (MissingSchema, InvalidSchema):
        # If path is a local file path, try to open the file (generate hash if none provided)
        with open(path, 'rb') as fobj:
            for chunk in iter(lambda: fobj.read(2097152), b""):
                write_to_file.write(chunk)
                hash.update(chunk)

    assert write_to_file.tell() > 0, "File failed to write (corrupted)."

    return hash

def copy_file_to_storage(filename, srcfile, delete_original=False):
    # Some files might have been closed, so only filepath will work
    if isinstance(srcfile, str):
        srcfile = open(srcfile, 'rb')

    # Write file to local storage
    with open(config.get_storage_path(filename), 'wb') as destf:
        if delete_original:
            shutil.move(srcfile.name, destf.name)
        else:
            shutil.copyfileobj(srcfile, destf)

def get_hash(filepath):
    hash = hashlib.md5()
    with open(filepath, 'rb') as fobj:
        for chunk in iter(lambda: fobj.read(2097152), b""):
            hash.update(chunk)
    return hash.hexdigest()


def compress_video_file(filename, ffmpeg_settings):
    ffmpeg_settings = ffmpeg_settings or {}
    key = generate_key("COMPRESSED", filename, settings=ffmpeg_settings, default=" (default compression)")

    if not config.UPDATE and FILECACHE.get(key):
        return FILECACHE.get(key).decode('utf-8')

    config.LOGGER.info("\t--- Compressing {}".format(filename))

    tempf = tempfile.NamedTemporaryFile(suffix=".{}".format(file_formats.MP4), delete=False)
    tempf.close() # Need to close so pressure cooker can write to file
    compress_video(config.get_storage_path(filename), tempf.name, overwrite=True, **ffmpeg_settings)
    filename = "{}.{}".format(get_hash(tempf.name), file_formats.MP4)

    copy_file_to_storage(filename, tempf.name)
    os.unlink(tempf.name)
    FILECACHE.set(key, bytes(filename, "utf-8"))
    return filename

def download_from_web(web_url, download_settings):
    key = generate_key("DOWNLOADED", web_url, settings=download_settings)
    if not config.UPDATE and FILECACHE.get(key):
        return FILECACHE.get(key).decode('utf-8')

    # Get hash of web_url to act as temporary storage name
    url_hash = hashlib.md5()
    url_hash.update(web_url.encode('utf-8'))
    destination_path = os.path.join(tempfile.gettempdir(), "{}.{}".format(url_hash.hexdigest(), file_formats.MP4))
    download_settings["outtmpl"] = destination_path
    try:
        os.remove(destination_path)
    except Exception:
        pass

    with youtube_dl.YoutubeDL(download_settings) as ydl:
        ydl.download([web_url])
        filename = "{}.{}".format(get_hash(destination_path), file_formats.MP4)

        # Write file to local storage
        with open(destination_path, "rb") as dlf, open(config.get_storage_path(filename), 'wb') as destf:
            shutil.copyfileobj(dlf, destf)

        FILECACHE.set(key, bytes(filename, "utf-8"))
        return filename

class ThumbnailPresetMixin(object):

    def get_preset(self):
        if isinstance(self.node, ChannelNode):
            return format_presets.CHANNEL_THUMBNAIL
        elif isinstance(self.node, VideoNode):
            return format_presets.VIDEO_THUMBNAIL
        elif isinstance(self.node, AudioNode):
            return format_presets.AUDIO_THUMBNAIL
        elif isinstance(self.node, DocumentNode):
            return format_presets.DOCUMENT_THUMBNAIL
        elif isinstance(self.node, ExerciseNode):
            return format_presets.EXERCISE_THUMBNAIL
        elif isinstance(self.node, HTML5AppNode):
            return format_presets.HTML5_THUMBNAIL
        else:
            raise UnknownFileTypeError("Thumbnails are not supported for node kind.")

class File(object):
    original_filename = None
    node = None
    error = None
    default_ext = None
    filename = None
    language = None
    assessment_item = None

    def __init__(self, preset=None, language=None, default_ext=None, source_url=None):
        self.preset = preset
        self.language = language
        self.default_ext = default_ext or self.default_ext
        self.source_url = source_url

    def validate(self):
        pass

    def get_preset(self):
        if self.preset:
            return self.preset
        raise NotImplementedError("preset must be set if preset isn't specified when creating File object")

    def get_filename(self):
        if self.filename:
            return self.filename
        return self.process_file()

    def to_dict(self):
        filename = self.get_filename()

        # If file was successfully downloaded, return dict
        # Otherwise return None
        if filename:
            if os.path.isfile(config.get_storage_path(filename)):
                return {
                    'size' : os.path.getsize(config.get_storage_path(filename)),
                    'preset' : self.get_preset(),
                    'filename' : filename,
                    'original_filename' : self.original_filename,
                    'language' : self.language,
                    'source_url': self.source_url,
                }
            else:
                config.LOGGER.warning("File not found: {}".format(config.get_storage_path(filename)))

        return None

    def process_file(self):
        # Overwrite in subclasses
        pass

class DownloadFile(File):
    allowed_formats = []

    def __init__(self, path, **kwargs):
        self.path = path.strip()
        super(DownloadFile, self).__init__(**kwargs)

    def validate(self):
        assert self.path, "{} must have a path".format(self.__class__.__name__)
        _basename, ext = os.path.splitext(self.path)
        plain_ext = ext.lstrip('.')
        # don't validate for single-digit extension, or no extension
        if len(plain_ext) > 1:
            assert plain_ext in self.allowed_formats, "{} must have one of the following extensions: {} (instead, got '{}' from '{}')".format(self.__class__.__name__, self.allowed_formats, plain_ext, self.path)

    def process_file(self):
        try:
            self.filename = download(self.path, default_ext=self.default_ext)
            config.LOGGER.info("\t--- Downloaded {}".format(self.filename))
            return self.filename
        # Catch errors related to reading file path and handle silently
        except (HTTPError, ConnectionError, InvalidURL, UnicodeDecodeError, UnicodeError, InvalidSchema, IOError, AssertionError) as err:
            self.error = err
            config.FAILED_FILES.append(self)

    def __str__(self):
        return self.path


class ThumbnailFile(ThumbnailPresetMixin, DownloadFile):
    default_ext = file_formats.PNG
    allowed_formats = [file_formats.JPG, file_formats.JPEG, file_formats.PNG]

class AudioFile(DownloadFile):
    default_ext = file_formats.MP3
    allowed_formats = [file_formats.MP3]

    def get_preset(self):
        return self.preset or format_presets.AUDIO

class DocumentFile(DownloadFile):
    default_ext = file_formats.PDF
    allowed_formats = [file_formats.PDF]

    def get_preset(self):
        return self.preset or format_presets.DOCUMENT

class HTMLZipFile(DownloadFile):
    default_ext = file_formats.HTML5
    allowed_formats = [file_formats.HTML5]

    def get_preset(self):
        return self.preset or format_presets.HTML5_ZIP

    def validate(self):
        super(HTMLZipFile, self).validate()

        # make sure index.html exists
        with zipfile.ZipFile(self.path) as zf:
            try:
                info = zf.getinfo('index.html')
            except KeyError:
                assert False, "Assumption Failed: HTML zip must have an `index.html` file at topmost level"

class ExtractedVideoThumbnailFile(ThumbnailFile):

    def process_file(self):
        self.filename = self.derive_thumbnail()
        config.LOGGER.info("\t--- Extracted thumbnail {}".format(self.filename))
        return self.filename

    def derive_thumbnail(self):
        key = "EXTRACTED: {}".format(self.path)
        if not config.UPDATE and FILECACHE.get(key):
            return FILECACHE.get(key).decode('utf-8')

        config.LOGGER.info("\t--- Extracting thumbnail from {}".format(self.path))
        tempf = tempfile.NamedTemporaryFile(suffix=".{}".format(file_formats.PNG), delete=False)
        tempf.close()
        extract_thumbnail_from_video(self.path, tempf.name, overwrite=True)
        filename = "{}.{}".format(get_hash(tempf.name), file_formats.PNG)

        copy_file_to_storage(filename, tempf.name)
        os.unlink(tempf.name)
        FILECACHE.set(key, bytes(filename, "utf-8"))
        return filename

class VideoFile(DownloadFile):
    default_ext = file_formats.MP4
    allowed_formats = [file_formats.MP4]

    def __init__(self, path, ffmpeg_settings=None, **kwargs):
        self.ffmpeg_settings = ffmpeg_settings
        super(VideoFile, self).__init__(path, **kwargs)

    def get_preset(self):
        return self.preset or guess_video_preset_by_resolution(config.get_storage_path(self.filename))

    def process_file(self):
        try:
            # Get copy of video before compression (if specified)
            self.filename = super(VideoFile, self).process_file()
            if self.filename and (self.ffmpeg_settings or config.COMPRESS):
                self.filename = compress_video_file(self.filename, self.ffmpeg_settings)
                config.LOGGER.info("\t--- Compressed {}".format(self.filename))
            return self.filename
        # Catch errors related to ffmpeg and handle silently
        except (BrokenPipeError, CalledProcessError, IOError) as err:
            self.error = err
            config.FAILED_FILES.append(self)


class WebVideoFile(File):
    # In future, look into postprocessors and progress_hooks
    def __init__(self, web_url, download_settings=None, high_resolution=True, maxheight=None, **kwargs):
        self.web_url = web_url
        self.download_settings = download_settings or {}
        if "format" not in self.download_settings:
            maxheight = maxheight or (720 if high_resolution else 480)
            self.download_settings['format'] = "bestvideo[height<={maxheight}][ext=mp4]+bestaudio[ext=m4a]/best[height<={maxheight}][ext=mp4]".format(maxheight=maxheight)
        # self.download_settings["outtmpl"] = "%(title)s (%(format)s)-%(display_id)s.%(ext)s"

        super(WebVideoFile, self).__init__(**kwargs)

    def get_preset(self):
        return self.preset or guess_video_preset_by_resolution(config.get_storage_path(self.filename))

    def process_file(self):
        try:
            self.filename = download_from_web(self.web_url, self.download_settings)
            config.LOGGER.info("\t--- Downloaded (YouTube) {}".format(self.filename))
            return self.filename
        except youtube_dl.utils.DownloadError as err:
            self.error = str(err)
            config.FAILED_FILES.append(self)


class YouTubeVideoFile(WebVideoFile):
    def __init__(self, youtube_id, **kwargs):
        super(YouTubeVideoFile, self).__init__('http://www.youtube.com/watch?v={}'.format(youtube_id), **kwargs)

class YouTubeSubtitleFile(File):
    def __init__(self, youtube_id, language=None, **kwargs):
        self.youtube_id = youtube_id
        super(YouTubeSubtitleFile, self).__init__(language=language, **kwargs)
        assert self.language, "Subtitles must have a language"

    def get_preset(self):
        return self.preset or format_presets.VIDEO_SUBTITLE

    def process_file(self):
        self.filename = self.download_subtitle()
        config.LOGGER.info("\t--- Downloaded subtitle {}".format(self.filename))
        return self.filename

    def download_subtitle(self):
        key = "DOWNLOADED YOUTUBE {}-{}".format(self.youtube_id, self.language)
        if not config.UPDATE and FILECACHE.get(key):
            return FILECACHE.get(key).decode('utf-8')

        url_hash = hashlib.md5()
        url_hash.update(self.youtube_id.encode('utf-8'))
        destination_path = os.path.join(tempfile.gettempdir(), "{}".format(url_hash.hexdigest()))
        try:
            os.remove(destination_path)
        except Exception:
            pass

        settings = {
            'skip_download': True,
            'writesubtitles': True,
            'subtitleslangs': [self.language],
            'outtmpl': destination_path,
            'subtitlesformat': "best[ext={}]".format(file_formats.VTT),
            'quiet': True,
        }

        with youtube_dl.YoutubeDL(settings) as ydl:
            ydl.download(['http://www.youtube.com/watch?v={}'.format(self.youtube_id)])
            youtube_download_path = "{destpath}.{lang}.{ext}".format(destpath=destination_path, lang=self.language, ext=file_formats.VTT)

            filename = "{}.{}".format(get_hash(youtube_download_path), file_formats.VTT)

            # Write file to local storage
            with open(youtube_download_path, "rb") as dlf, open(config.get_storage_path(filename), 'wb') as destf:
                shutil.copyfileobj(dlf, destf)

            FILECACHE.set(key, bytes(filename, "utf-8"))
            return filename

class SubtitleFile(DownloadFile):
    default_ext = file_formats.VTT
    allowed_formats = [file_formats.VTT]

    def __init__(self, path, **kwargs):
        super(SubtitleFile, self).__init__(path, **kwargs)
        assert self.language, "Subtitles must have a language"

    def get_preset(self):
        return self.preset or format_presets.VIDEO_SUBTITLE


class Base64ImageFile(ThumbnailPresetMixin, File):

    def __init__(self, encoding, **kwargs):
        self.encoding = encoding
        super(Base64ImageFile, self).__init__(**kwargs)

    def process_file(self):
        """ process_file: Writes base64 encoding to file
            Args: None
            Returns: filename
        """
        self.filename = self.convert_base64_to_file()
        config.LOGGER.info("\t--- Converted base64 image to {}".format(self.filename))
        return self.filename

    def convert_base64_to_file(self):
        # Get hash of content for cache key
        hashed_content = hashlib.md5()
        hashed_content.update(self.encoding.encode('utf-8'))
        key = "ENCODED: {} (base64 encoded)".format(hashed_content.hexdigest())

        if not config.UPDATE and FILECACHE.get(key):
            return FILECACHE.get(key).decode('utf-8')

        config.LOGGER.info("\tConverting base64 to file")

        extension = get_base64_encoding(self.encoding).group(1)
        assert extension in [file_formats.PNG, file_formats.JPG, file_formats.JPEG], "Base64 files must be images in jpg or png format"

        tempf = tempfile.NamedTemporaryFile(suffix=".{}".format(extension), delete=False)
        tempf.close()
        write_base64_to_file(self.encoding, tempf.name)
        filename = "{}.{}".format(get_hash(tempf.name), file_formats.PNG)

        copy_file_to_storage(filename, tempf.name)
        os.unlink(tempf.name)
        FILECACHE.set(key, bytes(filename, "utf-8"))
        return filename

class _ExerciseBase64ImageFile(Base64ImageFile):
    default_ext = file_formats.PNG

    def get_preset(self):
        return self.preset or format_presets.EXERCISE_IMAGE

    def get_replacement_str(self):
        return self.get_filename() or self.encoding

class _ExerciseImageFile(DownloadFile):
    default_ext = file_formats.PNG

    def get_replacement_str(self):
        return self.get_filename() or self.path

    def get_preset(self):
        return self.preset or format_presets.EXERCISE_IMAGE

class _ExerciseGraphieFile(DownloadFile):
    default_ext = file_formats.GRAPHIE

    def __init__(self, path, **kwargs):
        self.original_filename = path.split("/")[-1].split(".")[0]
        super(_ExerciseGraphieFile, self).__init__(path, **kwargs)

    def get_preset(self):
        return self.preset or format_presets.EXERCISE_GRAPHIE

    def get_replacement_str(self):
        return self.path.split("/")[-1].split(".")[0] or self.path

    def process_file(self):
        """ download: download a web+graphie file
            Args: None
            Returns: None
        """
        try:
            self.filename = self.generate_graphie_file()
            config.LOGGER.info("\t--- Generated graphie {}".format(self.filename))
            return self.filename
        # Catch errors related to reading file path and handle silently
        except (HTTPError, ConnectionError, InvalidURL, UnicodeDecodeError, UnicodeError, InvalidSchema, IOError) as err:
            self.error = err
            config.FAILED_FILES.append(self)

    def generate_graphie_file(self):
        key = "GRAPHIE: {}".format(self.path)

        if not config.UPDATE and FILECACHE.get(key):
            return FILECACHE.get(key).decode('utf-8')

        # Create graphie file combining svg and json files
        with tempfile.TemporaryFile() as tempf:
            # Initialize hash and files
            delimiter = bytes(exercises.GRAPHIE_DELIMITER, 'UTF-8')
            config.LOGGER.info("\tDownloading graphie {}".format(self.original_filename))


            # Write to graphie file
            hash = write_and_get_hash(self.path + ".svg", tempf)
            tempf.write(delimiter)
            hash.update(delimiter)
            hash = write_and_get_hash(self.path + "-data.json", tempf, hash)
            tempf.seek(0)
            filename = "{}.{}".format(hash.hexdigest(), file_formats.GRAPHIE)

            copy_file_to_storage(filename, tempf)

            FILECACHE.set(key, bytes(filename, "utf-8"))
            return filename

# VectorizedVideoFile
# TiledThumbnailFile
# UniversalSubsSubtitleFile
# class TiledThumbnailFile(ThumbnailFile):
#     def __init__(self, sources):
#         assert len(sources) == 4, "Please provide 4 sources for creating tiled thumbnail"
#         self.sources = [ThumbnailFile(path=source) if isinstance(source, str) else source for source in sources]

#     def get_file(self):
#         images = [source.get_file() for source in self.sources]
#         thumbnail_storage_path = create_tiled_image(images)

# class UniversalSubsSubtitleFile(SubtitleFile):
#     def __init__(self, us_id, language):
#         response = sess.get("http://usubs.org/api/{}".format(us_id))
#         path = json.loads(response.content)["subtitle_url"]
#         return super(UniversalSubsSubtitleFile, self).__init__(path=path, language=language)
