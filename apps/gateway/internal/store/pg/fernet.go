// Package pg · fernet.go
//
// Fernet 对称加密/解密器（与 Python `cryptography.fernet.Fernet` 字节级兼容）。
//
// Fernet 是一种基于 AES-128-CBC + HMAC-SHA256 的消息认证加密方案：
//   1. URL-safe base64 编码的 32 字节主密钥；
//   2. 通过 HMAC-SHA256 从主密钥派生 signing_key 与 encryption_key（IV 长度 16 字节）；
//   3. 消息格式 = Version(1) || Timestamp(8) || IV(16) || Ciphertext(*) || HMAC(32)，全部 base64；
//   4. CBC + PKCS#7 padding 解密明文；
//   5. 用 signing_key 计算 (version||timestamp||iv||ciphertext) 的 HMAC-SHA256 并与尾部 32 字节对比。
//
// 该实现用于在 Go gateway 中解密 platform-api 用 Fernet 加密写入 `gw_channel.credential_enc`
// 的上游 provider API Key，让 Go gateway 能像 Python relay 一样真正把请求发给 OpenAI /
// SiliconFlow 等上游服务。
package pg

import (
	"crypto/aes"
	"crypto/cipher"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/base64"
	"encoding/binary"
	"errors"
	"fmt"
	"time"
)

// fernetVersionByte 与 fernetTimestampBytes、fernetIVBytes、fernetHMACBytes 与
// Python cryptography 库保持一致。
const (
	fernetVersionByte = 0x80
	fernetTimestampBytes = 8
	fernetIVBytes = 16
	fernetHMACBytes = 32
	minCiphertextBlocks = 1
)

// fernetCipher 用于从 32 字节主密钥派生加密/签名密钥并解密密文。
type fernetCipher struct {
	signingKey    []byte
	encryptionKey []byte
}

// newFernetCipher 把 url-safe base64 编码的 32 字节主密钥展开为 cipher 实例。
// Python cryptography.fernet.Fernet 的实际密钥派生：
//   signing_key    = master[:16]
//   encryption_key = master[16:]
// （与 OpenBSD BCrypt 规范一致，不需要额外的 HKDF/HMAC 步骤）。
func newFernetCipher(masterB64 string) (*fernetCipher, error) {
	master, err := base64.RawURLEncoding.DecodeString(masterB64)
	if err != nil {
		// 兼容标准 base64（带填充）
		master, err = base64.URLEncoding.DecodeString(masterB64)
		if err != nil {
			return nil, fmt.Errorf("invalid fernet key encoding: %w", err)
		}
	}
	if len(master) != 32 {
		return nil, fmt.Errorf("fernet master key must be 32 url-safe base64 bytes, got %d", len(master))
	}
	return &fernetCipher{signingKey: append([]byte{}, master[:16]...), encryptionKey: append([]byte{}, master[16:]...)}, nil
}

// DecryptFernetToken 用 url-safe base64 编码的 32 字节主密钥解密一个 Fernet
// token，返回明文。导出给配置同步（configsync）等模块复用同一套与 Python
// cryptography 字节级兼容的实现；maxClockSkew 传 0 表示不校验时间戳。
func DecryptFernetToken(masterB64, token string, maxClockSkew time.Duration) ([]byte, error) {
	cipher, err := newFernetCipher(masterB64)
	if err != nil {
		return nil, err
	}
	return cipher.Decrypt(token, maxClockSkew)
}

// hmacSum 计算 HMAC-SHA256(key, msg) 并返回 32 字节摘要。
func hmacSum(key, msg []byte) []byte {
	mac := hmac.New(sha256.New, key)
	mac.Write(msg)
	return mac.Sum(nil)
}

// Decrypt 解密一个 base64 编码的 Fernet token 并返回明文。可选 maxClockSkew
// 用于验证时间戳（生产中通常给 60 秒），传入 0 表示不校验时间戳。
func (c *fernetCipher) Decrypt(token string, maxClockSkew time.Duration) ([]byte, error) {
	raw, err := base64.RawURLEncoding.DecodeString(token)
	if err != nil {
		raw, err = base64.URLEncoding.DecodeString(token)
		if err != nil {
			return nil, fmt.Errorf("decode fernet token: %w", err)
		}
	}
	minLen := 1 + fernetTimestampBytes + fernetIVBytes + aes.BlockSize + fernetHMACBytes
	if len(raw) < minLen {
		return nil, fmt.Errorf("fernet token too short: %d bytes", len(raw))
	}
	// 1) 校验 version byte
	if raw[0] != fernetVersionByte {
		return nil, fmt.Errorf("unsupported fernet version: 0x%02x", raw[0])
	}
	// 2) 校验 HMAC-SHA256(signingKey, version||timestamp||iv||ciphertext)
	macRegion := raw[:len(raw)-fernetHMACBytes]
	got := raw[len(raw)-fernetHMACBytes:]
	want := hmacSum(c.signingKey, macRegion)
	if !hmac.Equal(got, want) {
		return nil, errors.New("fernet token hmac mismatch")
	}
	// 3) 解析 timestamp（可选 clock-skew 校验）
	ts := int64(binary.BigEndian.Uint64(raw[1 : 1+fernetTimestampBytes]))
	if maxClockSkew > 0 {
		now := time.Now().Unix()
		if ts > now+int64(maxClockSkew.Seconds()) || ts < now-int64(maxClockSkew.Seconds()) {
			return nil, fmt.Errorf("fernet token expired: ts=%d now=%d", ts, now)
		}
	}
	// 4) 解密
	iv := raw[1+fernetTimestampBytes : 1+fernetTimestampBytes+fernetIVBytes]
	ct := raw[1+fernetTimestampBytes+fernetIVBytes : len(raw)-fernetHMACBytes]
	if len(ct) < aes.BlockSize || len(ct)%aes.BlockSize != 0 {
		return nil, fmt.Errorf("invalid fernet ciphertext length: %d", len(ct))
	}
	block, err := aes.NewCipher(c.encryptionKey)
	if err != nil {
		return nil, fmt.Errorf("aes init: %w", err)
	}
	plain := make([]byte, len(ct))
	mode := newCBCCrypter(block, iv)
	mode.CryptBlocks(plain, ct)
	// 5) 移除 PKCS#7 padding
	padLen := int(plain[len(plain)-1])
	if padLen < 1 || padLen > aes.BlockSize {
		return nil, errors.New("invalid pkcs7 padding")
	}
	for i := 0; i < padLen; i++ {
		if plain[len(plain)-1-i] != byte(padLen) {
			return nil, errors.New("invalid pkcs7 padding bytes")
		}
	}
	return plain[:len(plain)-padLen], nil
}

// newCBCCrypter 创建一个与 crypto/cipher.BlockMode 同形状的 CBC 解密器包装。
// 之所以自己实现而不用 cipher.NewCBCDecrypter：方便调整 IV 取法并兼容旧式 Go。
func newCBCCrypter(b cipher.Block, iv []byte) cbcDecrypter {
	return cbcDecrypter{block: b, iv: append([]byte{}, iv...)}
}

type cbcDecrypter struct {
	block cipher.Block
	iv    []byte
}

// CryptBlocks 用 AES-CBC 解密整个 src 到 dst（len(dst) == len(src) 且为 16 的倍数）。
func (c cbcDecrypter) CryptBlocks(dst, src []byte) {
	if len(src)%aes.BlockSize != 0 {
		panic("input not full blocks")
	}
	if len(dst) < len(src) {
		panic("output smaller than input")
	}
	prev := c.iv
	for off := 0; off < len(src); off += aes.BlockSize {
		c.block.Decrypt(dst[off:off+aes.BlockSize], src[off:off+aes.BlockSize])
		for i := 0; i < aes.BlockSize; i++ {
			dst[off+i] ^= prev[i]
		}
		prev = src[off : off+aes.BlockSize]
	}
}