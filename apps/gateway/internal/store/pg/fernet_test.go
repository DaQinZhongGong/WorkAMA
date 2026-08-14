package pg

import (
	"context"
	"fmt"
	"os"
	"testing"

	"github.com/jackc/pgx/v5/pgxpool"
)

// TestFernetRoundtrip 直接用 Go 实现的 fernetCipher 验证 platform-api
// 用 Fernet 加密的 token 能被 Go 端解密。这是 v7.248 真实联通验证。
func TestFernetRoundtrip(t *testing.T) {
	// 平台部署默认 key（与 docker-compose.yml ENCRYPTION_KEY 一致）。
	master := os.Getenv("ENCRYPTION_KEY")
	if master == "" {
		master = "QkJCQkJCQkJCQkJCQkJCQkJCQkJCQkJCQkJCQkJCQkI="
	}
	cipher, err := newFernetCipher(master)
	if err != nil {
		t.Fatalf("newFernetCipher failed: %v", err)
	}
	// 从 gw_channel 真实查出的 Fernet token（v7.247 端到端测试时创建的）。
	const realToken = "gAAAAABqe1W2O8jKqXLFeI4-sNWtU4sRuYdSfEe5KH9Xhs0W6ZmYPnh0T9DnY9_Yokr7JviwKjzzW5jb2mFV9scrG9hVw57w-Fn8cpv2jekUOJanRVUolIc46YIYN12P_q-BboPpfP2A"
	plain, err := cipher.Decrypt(realToken, 0)
	if err != nil {
		t.Fatalf("decrypt failed: %v", err)
	}
	fmt.Printf("[TestFernetRoundtrip] decrypted=%q\n", string(plain))
	if len(plain) == 0 {
		t.Fatal("decrypted plaintext is empty")
	}
}

// TestFernetVsPG 端到端验证：从 PG 读 gw_channel.credential_enc 并解密。
func TestFernetVsPG(t *testing.T) {
	dsn := os.Getenv("DATABASE_URL")
	if dsn == "" {
		dsn = "postgres://workama:workama_dev@localhost:5432/workama?sslmode=disable"
	}
	pool, err := pgxpool.New(context.Background(), dsn)
	if err != nil {
		t.Skipf("DATABASE_URL not reachable: %v", err)
	}
	defer pool.Close()
	row := pool.QueryRow(context.Background(), "SELECT credential_enc FROM gw_channel WHERE id=$1", "chn_01KZRWDKS9J7XPMSXQE291ZWS0")
	var enc []byte
	if err := row.Scan(&enc); err != nil {
		t.Skipf("channel row not found: %v", err)
	}
	master := os.Getenv("ENCRYPTION_KEY")
	if master == "" {
		master = "QkJCQkJCQkJCQkJCQkJCQkJCQkJCQkJCQkJCQkJCQkI="
	}
	cipher, err := newFernetCipher(master)
	if err != nil {
		t.Fatalf("cipher: %v", err)
	}
	plain, err := cipher.Decrypt(string(enc), 0)
	if err != nil {
		t.Fatalf("decrypt: %v", err)
	}
	fmt.Printf("[TestFernetVsPG] credential_enc=%d bytes -> plain=%d bytes (%q...)\n", len(enc), len(plain), string(plain)[:min(40, len(plain))])
}

func min(a, b int) int {
	if a < b {
		return a
	}
	return b
}