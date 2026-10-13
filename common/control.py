"""Bounded BLE application controls. This framing is independent of TCP JSON."""
from dataclasses import dataclass
import hashlib
import struct

from common.sensor import _integer, decode_packet, SensorPacket

CONTROL_UUID = '6e1c0007-7a45-4dc4-b678-3f2d5a9c1001'
RESPONSE_UUID = '6e1c0008-7a45-4dc4-b678-3f2d5a9c1001'
COMMAND, SET_RATE = 1, 2
FILE_BEGIN, FILE_CHUNK, FILE_END, FILE_ABORT = 16, 17, 18, 19
RESPONSE_FLAG = 128
OK, INVALID, BUSY, ORDER, INTEGRITY, CONFLICT, UNSUPPORTED = range(7)
MIN_CONTROL_MTU, MAX_FILE_SIZE, MAX_CHUNK_SIZE = 64, 65536, 180
HEADER_SIZE = 14
_HEADER = struct.Struct('<2sBBBBII')
_OPCODES = (COMMAND, SET_RATE, FILE_BEGIN, FILE_CHUNK, FILE_END, FILE_ABORT)


@dataclass(frozen=True)
class ControlFrame:
    opcode: int
    device_id: int
    request_id: int
    offset: int = 0
    payload: bytes = b''
    status: int = 0


def chunk_size(mtu):
    _integer(mtu, MIN_CONTROL_MTU, 517, 'control MTU')
    return min(MAX_CHUNK_SIZE, mtu - 3 - HEADER_SIZE)


def _validate(frame, mtu):
    limit = chunk_size(mtu)
    if not isinstance(frame, ControlFrame):
        raise ValueError('expected ControlFrame')
    _integer(frame.opcode, 0, 255, 'opcode')
    _integer(frame.device_id, 1, 2, 'device_id')
    _integer(frame.request_id, 1, 0xffffffff, 'request_id')
    _integer(frame.offset, 0, MAX_FILE_SIZE, 'offset')
    _integer(frame.status, OK, UNSUPPORTED, 'status')
    if not isinstance(frame.payload, bytes) or len(frame.payload) > limit:
        raise ValueError('payload exceeds negotiated control size')
    response = bool(frame.opcode & RESPONSE_FLAG)
    opcode = frame.opcode & ~RESPONSE_FLAG
    if opcode not in _OPCODES or (not response and frame.status != OK):
        raise ValueError('unknown opcode or request status')
    if response and frame.status != OK:
        if frame.payload:
            raise ValueError('error response must not contain success payload')
        return
    if opcode == COMMAND:
        packet = decode_packet(frame.payload)
        if (frame.offset or packet.version != 2 or packet.device_id != frame.device_id
                or packet.seq != frame.request_id):
            raise ValueError('uncorrelated command sensor packet')
    elif opcode == SET_RATE:
        if frame.offset or len(frame.payload) != 2:
            raise ValueError('rate must be uint16')
        _integer(struct.unpack('<H', frame.payload)[0], 1, 200, 'rate')
    elif opcode == FILE_BEGIN:
        if response:
            if frame.payload:
                raise ValueError('begin ACK has no payload')
        else:
            parse_file_metadata(frame.payload)
            if frame.offset:
                raise ValueError('file begins at zero')
    elif opcode == FILE_CHUNK:
        if response:
            if frame.payload:
                raise ValueError('chunk ACK has no payload')
        elif not frame.payload or frame.offset + len(frame.payload) > MAX_FILE_SIZE:
            raise ValueError('empty or oversized file chunk')
    elif opcode == FILE_END:
        if response:
            length, _ = parse_file_metadata(frame.payload)
            if frame.offset != length:
                raise ValueError('file completion length mismatch')
        elif frame.payload:
            raise ValueError('end request has no payload')
    elif opcode == FILE_ABORT and (frame.payload or frame.offset):
        raise ValueError('abort has no payload and offset zero')


def encode_control(frame, *, mtu=517):
    _validate(frame, mtu)
    return _HEADER.pack(b'B7', 1, frame.opcode, frame.device_id, frame.status,
                        frame.request_id, frame.offset) + frame.payload


def decode_control(data, *, mtu=517):
    if not isinstance(data, (bytes, bytearray, memoryview)) or len(data) < HEADER_SIZE:
        raise ValueError('truncated control header')
    magic, version, opcode, device, status, request, offset = _HEADER.unpack_from(data)
    if magic != b'B7' or version != 1:
        raise ValueError('unsupported control magic/version')
    frame = ControlFrame(opcode, device, request, offset, bytes(data[HEADER_SIZE:]), status)
    _validate(frame, mtu)
    return frame


def file_metadata(data):
    if not isinstance(data, bytes) or not 1 <= len(data) <= MAX_FILE_SIZE:
        raise ValueError('file must contain 1..65536 bytes')
    return struct.pack('<I', len(data)) + hashlib.sha256(data).digest()


def parse_file_metadata(payload):
    if not isinstance(payload, bytes) or len(payload) != 36:
        raise ValueError('file metadata must contain length and SHA-256')
    length = struct.unpack_from('<I', payload)[0]
    _integer(length, 1, MAX_FILE_SIZE, 'file length')
    return length, payload[4:]


def transformed_values(values):
    packet = SensorPacket(1, 0, 0, 0, values, version=2)
    return tuple(-32768 if value == 32767 else value + 1 for value in packet.values)
