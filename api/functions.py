import binascii
from Crypto.Cipher import AES
import base64

class Functions:
    
    def __init__(self):
        self.BLOCK_SIZE = 16
    
    async def decryptLink(self, encrypted_data: str) -> str:
        encrypted_data = (encrypted_data or "").strip()
        if not encrypted_data:
            return ""

        # Gaana has used two encodings. Try the current offset-prefixed
        # payload first, then retain compatibility with older detail records.
        try:
            offset = int(encrypted_data[0])
            iv = encrypted_data[offset:offset + self.BLOCK_SIZE].encode("utf-8")
            cipher_text = encrypted_data[offset + self.BLOCK_SIZE:]
            cipher_bytes = base64.b64decode(cipher_text + ("=" * (-len(cipher_text) % 4)))
            decrypted = AES.new(b'gy1t#b@jl(b$wtme', AES.MODE_CBC, iv).decrypt(cipher_bytes)
            padding_length = decrypted[-1]
            if 1 <= padding_length <= self.BLOCK_SIZE:
                decrypted = decrypted[:-padding_length]
            value = decrypted.decode("utf-8").strip()
            if value:
                return value
        except (IndexError, ValueError, AttributeError, TypeError,
                UnicodeDecodeError, binascii.Error):
            pass

        try:
            legacy = base64.b64decode(encrypted_data + ("=" * (-len(encrypted_data) % 4)))
            decrypted = AES.new(
                b'g@1n!(f1#r.0$)&%', AES.MODE_CBC, b'asd!@#!@#@!12312'
            ).decrypt(legacy).decode("utf-8")
            padding_length = ord(decrypted[-1])
            return decrypted[:-padding_length] if 1 <= padding_length <= self.BLOCK_SIZE else decrypted
        except (IndexError, ValueError, AttributeError, TypeError,
                UnicodeDecodeError, binascii.Error):
            return ""

    async def findArtistNames(self, results: list) -> str:
        try:
            artists = []
            for i in results:
                artists.append(i['name'])
            return ', '.join(artists)
        except (KeyError, TypeError):
            return ""

    async def findArtistSeoKeys(self, results: list) -> str:
        try:
            seokeys = []
            for i in results:
                seokeys.append(i['seokey'])
            return ', '.join(seokeys)
        except (KeyError, TypeError):
            return ""

    async def findArtistIds(self, results: list) -> str:
        try:
            ids = []
            for i in results:
                ids.append(i['artist_id'])
            return ', '.join(ids)
        except (KeyError, TypeError):
            return ""

    async def findGenres(self, results: list) -> str:
        genres = []
        for i in results:
            try:
                genres.append(i['name'])
            except (KeyError, ValueError):
                continue
        return ', '.join(genres)

    async def isExplicit(self, explicit: int) -> bool:
        return explicit == 1
