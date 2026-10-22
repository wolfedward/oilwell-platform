"""Explicit versions only; v1 serialization remains owned by its original codec."""
import json
import struct
from . import result_codec as legacy
from .transactions import InversionError
from .result_codec_v2 import PreparedResultV2

PreparedResultAny=legacy.PreparedResult|PreparedResultV2


def validate_result(prepared):
    if type(prepared) not in (legacy.PreparedResult,PreparedResultV2):
        raise InversionError('result_type_invalid')
    prepared.__post_init__()


def decode_result(snapshot,metadata,chunks):
    if type(metadata) is not bytes or type(chunks) is not tuple or any(type(c) is not bytes for c in chunks):raise InversionError('result_type_invalid')
    if len(metadata)+sum(map(len,chunks))>legacy.MAX_RESULT:raise InversionError('result_size_limit')
    try:schema=json.loads(metadata)['schema']
    except (ValueError,TypeError,KeyError) as error:raise InversionError('result_identity_invalid') from error
    if schema=='inversion.result/1':return legacy.PreparedResult(snapshot,metadata,chunks)
    if schema=='inversion.result/2':return PreparedResultV2(snapshot,metadata,chunks)
    raise InversionError('result_schema_unknown')


def write_result(prepared,path):
    validate_result(prepared)
    legacy.write(prepared,path)


def read_result(path,snapshot):
    if path.stat().st_size>legacy.MAX_RESULT:raise InversionError('result_size_limit')
    items=[];total=0
    with path.open('rb') as stream:
        while header:=stream.read(8):
            total+=8
            if len(header)!=8:raise InversionError('result_truncated')
            size=struct.unpack('>Q',header)[0];total+=size
            if total>legacy.MAX_RESULT:raise InversionError('result_size_limit')
            item=stream.read(size)
            if len(item)!=size:raise InversionError('result_truncated')
            items.append(item)
    if not items:raise InversionError('result_truncated')
    try:schema=json.loads(items[0])['schema']
    except (ValueError,TypeError,KeyError) as error:raise InversionError('result_identity_invalid') from error
    if schema=='inversion.result/1':return legacy.read(path,snapshot)
    return decode_result(snapshot,items[0],tuple(items[1:]))
