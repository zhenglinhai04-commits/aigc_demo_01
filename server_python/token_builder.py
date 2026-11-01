# Server/token_builder.py
import time
import struct
import hmac
import hashlib
import base64
import random
from io import BytesIO

VERSION = "001"
VERSION_LENGTH = 3
APP_ID_LENGTH = 24

# 权限定义
PRIVILEGES = {
    "PrivPublishStream": 0,
    "privPublishAudioStream": 1,
    "privPublishVideoStream": 2,
    "privPublishDataStream": 3,
    "PrivSubscribeStream": 4,
}

class ByteBuf:
    def __init__(self, data=None):
        self.buffer = BytesIO(data) if data else BytesIO()

    def pack(self):
        return self.buffer.getvalue()

    def put_uint16(self, v):
        self.buffer.write(struct.pack('<H', v))
        return self

    def put_uint32(self, v):
        self.buffer.write(struct.pack('<I', v))
        return self

    def put_bytes(self, b):
        self.put_uint16(len(b))
        self.buffer.write(b)
        return self

    def put_string(self, s):
        return self.put_bytes(s.encode('utf-8'))

    def put_tree_map_uint32(self, m):
        if not m:
            self.put_uint16(0)
            return self
        
        self.put_uint16(len(m))
        for k, v in m.items():
            self.put_uint16(int(k))
            self.put_uint32(int(v))
        return self

class AccessToken:
    def __init__(self, app_id, app_key, room_id, user_id):
        self.app_id = app_id
        self.app_key = app_key
        self.room_id = room_id
        self.user_id = user_id
        self.issued_at = int(time.time())
        self.nonce = random.randint(0, 0xFFFFFFFF)
        self.expire_at = 0
        self.privileges = {}

    def add_privilege(self, privilege, expire_timestamp):
        self.privileges[privilege] = expire_timestamp
        
        if privilege == PRIVILEGES["PrivPublishStream"]:
            self.privileges[PRIVILEGES["privPublishVideoStream"]] = expire_timestamp
            self.privileges[PRIVILEGES["privPublishAudioStream"]] = expire_timestamp
            self.privileges[PRIVILEGES["privPublishDataStream"]] = expire_timestamp

    def expire_time(self, expire_timestamp):
        self.expire_at = expire_timestamp

    def pack_msg(self):
        buf = ByteBuf()
        buf.put_uint32(self.nonce)
        buf.put_uint32(self.issued_at)
        buf.put_uint32(self.expire_at)
        buf.put_string(self.room_id)
        buf.put_string(self.user_id)
        buf.put_tree_map_uint32(self.privileges)
        return buf.pack()

    def serialize(self):
        msg = self.pack_msg()
        # HMAC-SHA256 签名
        signature = hmac.new(
            self.app_key.encode('utf-8'), 
            msg, 
            hashlib.sha256
        ).digest()
        
        content = ByteBuf().put_bytes(msg).put_bytes(signature).pack()
        return VERSION + self.app_id + base64.b64encode(content).decode('utf-8')