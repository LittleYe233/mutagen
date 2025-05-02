# Copyright (C) 2012, 2013  Christoph Reiter
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.

"""Read and write Ogg Opus comments.

This module handles Opus files wrapped in an Ogg bitstream. The
first Opus stream found is used.

Based on http://tools.ietf.org/html/draft-terriberry-oggopus-01
"""

__all__ = ["OggOpus", "Open", "delete"]

import struct
from io import BytesIO

from mutagen import StreamInfo
from mutagen._util import get_size, loadfile, convert_error
from mutagen._tags import PaddingInfo
from mutagen._vorbis import VCommentDict
from mutagen.ogg import OggPage, OggFileType, error as OggError


# A workaround to calculate opus bitrate
# @see https://github.com/quodlibet/mutagen/issues/670#issue-2807751962
def _read_ogg_page(f):
    # Read the Ogg page header (27 bytes)
    header = f.read(27)
    if len(header) < 27:
        return None  # End of file or error

    # Unpack the header
    try:
        (capture_pattern, version, header_type, granule_position,
         serial_number, page_sequence_no, checksum, page_segments) = \
            struct.unpack('<4sBsqIIIB', header)
    except struct.error:
        raise ValueError("Not a valid Ogg file")

    # Check for the Ogg capture pattern
    if capture_pattern != b'OggS' or version != 0:
        raise ValueError("Not a valid Ogg file")

    # Read the segment table
    segment_table = f.read(page_segments)
    if len(segment_table) < page_segments:
        return None  # End of file or error

    # Read the segment data
    segment_sizes = [seg for seg in segment_table]
    body_size = sum(segment_sizes)
    body = f.read(body_size)

    return {'header': header, 'body': body, 'serial_number': serial_number,
            'granule_position': granule_position, 'header_type': header_type,
            'page_sequence_no': page_sequence_no,
            'page_segments': page_segments}

def _get_opus_stream_size(f):
    offset = f.tell()
    f.seek(0)
    header_count = 0
    serial = None
    size = 0
    last_page_size = None
    eos = False

    while True:
        page = _read_ogg_page(f)
        if page is None:
            break  # End of file or error
        # Read pages until first Opus header page (identification header)
        elif header_count == 0: # Identification header
            if page['body'][:8] != b'OpusHead':  # Page from other stream?
                continue
            else:  # Found it
                serial = page['serial_number']
                header_count += 1
        # Check for expected second Opus header page (comment header)
        elif header_count == 1 and page['serial_number'] == serial:
            if page['body'][:8] != b'OpusTags':
                raise ValueError("Not a valid Opus file")
            else:
                header_count += 1
        # Read pages until first page with audio data
        elif header_count == 2 and page['serial_number'] == serial:
            if int.from_bytes(page['header_type'], 'little') & 0x01:
                continue  # Contiunation of second Opus header page
            elif page['granule_position'] > 0:  # Found it
                header_count += 1
                last_page_size = len(page['body'])
                size += last_page_size
        # Read remaining pages with audio data
        elif page['granule_position'] > 0 and page['serial_number'] == serial:
            last_page_size = len(page['body'])
            size += last_page_size
            # If page is last page (end of stream), we are done
            if int.from_bytes(page['header_type'], 'little') & 0x04:
                eos = True
                break

    # If last page did not contain "end of stream" flag, it was incomplete
    if not eos:
        size -= last_page_size

    f.seek(offset)

    return size


def _opus_bitrate(fileobj, length: int) -> int:
    '''Calculate the bitrate (bit/s) of an opus file.

    :param fileobj: object of an open file
    :param length: length of opus file reported by OggOpusInfo
    '''

    size = _get_opus_stream_size(fileobj)
    return int(size * 8.0 / length)


# Original mutagen code
class error(OggError):
    pass


class OggOpusHeaderError(error):
    pass


class OggOpusInfo(StreamInfo):
    """OggOpusInfo()

    Ogg Opus stream information.

    Attributes:
        length (`float`): File length in seconds, as a float
        channels (`int`): Number of channels
        bitrate (`int`): bitrate in bits per second as an int
    """

    length = 0
    channels = 0

    def __init__(self, fileobj):
        page = OggPage(fileobj)
        while not page.packets[0].startswith(b"OpusHead"):
            page = OggPage(fileobj)

        self.serial = page.serial

        if not page.first:
            raise OggOpusHeaderError(
                "page has ID header, but doesn't start a stream")

        (version, self.channels, pre_skip, orig_sample_rate, output_gain,
         channel_map) = struct.unpack("<BBHIhB", page.packets[0][8:19])

        self.__pre_skip = pre_skip

        # only the higher 4 bits change on incombatible changes
        major = version >> 4
        if major != 0:
            raise OggOpusHeaderError("version %r unsupported" % major)

    def _post_tags(self, fileobj):
        page = OggPage.find_last(fileobj, self.serial, finishing=True)
        if page is None:
            raise OggOpusHeaderError
        self.length = (page.position - self.__pre_skip) / float(48000)
        # Inject bitrate calculation
        self.bitrate = _opus_bitrate(fileobj, self.length)

    def pprint(self):
        return u"Ogg Opus, %.2f seconds" % (self.length)


class OggOpusVComment(VCommentDict):
    """Opus comments embedded in an Ogg bitstream."""

    def __get_comment_pages(self, fileobj, info):
        # find the first tags page with the right serial
        page = OggPage(fileobj)
        while ((info.serial != page.serial) or
                not page.packets[0].startswith(b"OpusTags")):
            page = OggPage(fileobj)

        # get all comment pages
        pages = [page]
        while not (pages[-1].complete or len(pages[-1].packets) > 1):
            page = OggPage(fileobj)
            if page.serial == pages[0].serial:
                pages.append(page)

        return pages

    def __init__(self, fileobj, info):
        pages = self.__get_comment_pages(fileobj, info)
        data = OggPage.to_packets(pages)[0][8:]  # Strip OpusTags
        fileobj = BytesIO(data)
        super(OggOpusVComment, self).__init__(fileobj, framing=False)
        self._padding = len(data) - self._size

        # in case the LSB of the first byte after v-comment is 1, preserve the
        # following data
        padding_flag = fileobj.read(1)
        if padding_flag and ord(padding_flag) & 0x1:
            self._pad_data = padding_flag + fileobj.read()
            self._padding = 0  # we have to preserve, so no padding
        else:
            self._pad_data = b""

    def _inject(self, fileobj, padding_func):
        fileobj.seek(0)
        info = OggOpusInfo(fileobj)
        old_pages = self.__get_comment_pages(fileobj, info)

        packets = OggPage.to_packets(old_pages)
        vcomment_data = b"OpusTags" + self.write(framing=False)

        if self._pad_data:
            # if we have padding data to preserver we can't add more padding
            # as long as we don't know the structure of what follows
            packets[0] = vcomment_data + self._pad_data
        else:
            content_size = get_size(fileobj) - len(packets[0])  # approx
            padding_left = len(packets[0]) - len(vcomment_data)
            info = PaddingInfo(padding_left, content_size)
            new_padding = info._get_padding(padding_func)
            packets[0] = vcomment_data + b"\x00" * new_padding

        new_pages = OggPage._from_packets_try_preserve(packets, old_pages)
        OggPage.replace(fileobj, old_pages, new_pages)


class OggOpus(OggFileType):
    """OggOpus(filething)

    An Ogg Opus file.

    Arguments:
        filething (filething)

    Attributes:
        info (`OggOpusInfo`)
        tags (`mutagen._vorbis.VCommentDict`)

    """

    _Info = OggOpusInfo
    _Tags = OggOpusVComment
    _Error = OggOpusHeaderError
    _mimes = ["audio/ogg", "audio/ogg; codecs=opus"]

    info = None
    tags = None

    @staticmethod
    def score(filename, fileobj, header):
        return (header.startswith(b"OggS") * (b"OpusHead" in header))


Open = OggOpus


@convert_error(IOError, error)
@loadfile(method=False, writable=True)
def delete(filething):
    """ delete(filething)

    Arguments:
        filething (filething)
    Raises:
        mutagen.MutagenError

    Remove tags from a file.
    """

    t = OggOpus(filething)
    filething.fileobj.seek(0)
    t.delete(filething)
