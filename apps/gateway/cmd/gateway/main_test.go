package main

import (
	"strings"
	"testing"
)

// TestEnvReturnsEnvValueWhenSet 验证环境变量已设置且非空时返回其值。
func TestEnvReturnsEnvValueWhenSet(t *testing.T) {
	t.Setenv("TEST_GATEWAY_ENV_VAR", "production-value")
	if got := env("TEST_GATEWAY_ENV_VAR", "fallback"); got != "production-value" {
		t.Fatalf("env() = %q, want %q", got, "production-value")
	}
}

// TestEnvReturnsFallbackWhenEmpty 验证环境变量设置为空字符串时返回 fallback。
func TestEnvReturnsFallbackWhenEmpty(t *testing.T) {
	t.Setenv("TEST_GATEWAY_ENV_VAR", "")
	if got := env("TEST_GATEWAY_ENV_VAR", "fallback"); got != "fallback" {
		t.Fatalf("env() = %q, want %q", got, "fallback")
	}
}

// TestEnvReturnsFallbackWhenUnset 验证环境变量未设置时返回 fallback。
func TestEnvReturnsFallbackWhenUnset(t *testing.T) {
	// 使用一个极不可能存在的变量名，确保未设置状态
	name := "TEST_GATEWAY_ENV_VAR_DEFINITELY_UNSET_12345"
	if got := env(name, "default-value"); got != "default-value" {
		t.Fatalf("env() = %q, want %q", got, "default-value")
	}
}

// TestEnvReturnsFallbackForVariousValues 验证不同类型的 fallback 值都能正确返回。
func TestEnvReturnsFallbackForVariousValues(t *testing.T) {
	tests := []struct {
		name     string
		fallback string
	}{
		{name: "empty fallback", fallback: ""},
		{name: "url fallback", fallback: "http://localhost:8000"},
		{name: "port fallback", fallback: "8080"},
		{name: "token fallback", fallback: "unused-generic-fallback"},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			name := "TEST_GATEWAY_ENV_UNSET_" + test.name
			if got := env(name, test.fallback); got != test.fallback {
				t.Fatalf("env() = %q, want %q", got, test.fallback)
			}
		})
	}
}

func TestResolveInternalTokenRequiresEnv(t *testing.T) {
	t.Setenv("INTERNAL_TOKEN", "")
	t.Setenv("WORKAMA_ENV", "development")
	if _, err := resolveInternalToken(); err == nil || !strings.Contains(err.Error(), "required") {
		t.Fatalf("expected required error, got %v", err)
	}
}

func TestResolveInternalTokenRejectsPlaceholder(t *testing.T) {
	t.Setenv("INTERNAL_TOKEN", "change-this-internal-token")
	t.Setenv("WORKAMA_ENV", "development")
	if _, err := resolveInternalToken(); err == nil || !strings.Contains(err.Error(), "placeholder") {
		t.Fatalf("expected placeholder error, got %v", err)
	}
}

func TestResolveInternalTokenAllowsDevDefaultOutsideProduction(t *testing.T) {
	t.Setenv("INTERNAL_TOKEN", "workama-dev-internal-token-2026")
	t.Setenv("WORKAMA_ENV", "development")
	got, err := resolveInternalToken()
	if err != nil {
		t.Fatalf("dev default should be accepted outside production: %v", err)
	}
	if got != "workama-dev-internal-token-2026" {
		t.Fatalf("got %q", got)
	}
}

func TestResolveInternalTokenRejectsDevDefaultInProduction(t *testing.T) {
	t.Setenv("INTERNAL_TOKEN", "workama-dev-internal-token-2026")
	t.Setenv("WORKAMA_ENV", "production")
	if _, err := resolveInternalToken(); err == nil || !strings.Contains(err.Error(), "development default") {
		t.Fatalf("expected production rejection, got %v", err)
	}
}

func TestResolveInternalTokenAcceptsUniqueSecret(t *testing.T) {
	t.Setenv("INTERNAL_TOKEN", "unique-prod-internal-token-32bytes-min")
	t.Setenv("WORKAMA_ENV", "production")
	got, err := resolveInternalToken()
	if err != nil {
		t.Fatalf("unique secret should be accepted: %v", err)
	}
	if got != "unique-prod-internal-token-32bytes-min" {
		t.Fatalf("got %q", got)
	}
}

func TestResolveKeyPepperRequiresEnv(t *testing.T) {
	t.Setenv("KEY_PEPPER", "")
	t.Setenv("WORKAMA_ENV", "development")
	if _, err := resolveKeyPepper(); err == nil || !strings.Contains(err.Error(), "required") {
		t.Fatalf("expected required error, got %v", err)
	}
}

func TestResolveKeyPepperRejectsPlaceholderInProduction(t *testing.T) {
	for _, placeholder := range []string{"change-this-key-pepper", "change-this-pepper", "workama-local-key-pepper-change-before-production"} {
		t.Setenv("KEY_PEPPER", placeholder)
		t.Setenv("WORKAMA_ENV", "production")
		if _, err := resolveKeyPepper(); err == nil || !strings.Contains(err.Error(), "placeholder") {
			t.Fatalf("expected placeholder error for %q, got %v", placeholder, err)
		}
	}
}

func TestResolveKeyPepperAllowsPlaceholderInDevelopment(t *testing.T) {
	t.Setenv("KEY_PEPPER", "change-this-key-pepper")
	t.Setenv("WORKAMA_ENV", "development")
	if _, err := resolveKeyPepper(); err != nil {
		t.Fatalf("placeholder pepper tolerated in development: %v", err)
	}
}

func TestResolveKeyPepperAcceptsUniquePepper(t *testing.T) {
	t.Setenv("KEY_PEPPER", "unique-prod-pepper-32-bytes-minimum-ok")
	t.Setenv("WORKAMA_ENV", "production")
	got, err := resolveKeyPepper()
	if err != nil {
		t.Fatalf("unique pepper should be accepted: %v", err)
	}
	if got != "unique-prod-pepper-32-bytes-minimum-ok" {
		t.Fatalf("got %q", got)
	}
}

func TestResolveEncryptionKeyAllowsEmptyOutsideProduction(t *testing.T) {
	t.Setenv("ENCRYPTION_KEY", "")
	t.Setenv("WORKAMA_ENV", "development")
	if _, err := resolveEncryptionKey(); err != nil {
		t.Fatalf("empty key allowed outside production: %v", err)
	}
}

func TestResolveEncryptionKeyRequiresInProduction(t *testing.T) {
	t.Setenv("ENCRYPTION_KEY", "")
	t.Setenv("WORKAMA_ENV", "production")
	if _, err := resolveEncryptionKey(); err == nil || !strings.Contains(err.Error(), "required") {
		t.Fatalf("expected required error in production, got %v", err)
	}
}

func TestResolveEncryptionKeyRejectsWeakDefaultInProduction(t *testing.T) {
	t.Setenv("ENCRYPTION_KEY", "QkJCQkJCQkJCQkJCQkJCQkJCQkJCQkJCQkJCQkJCQkI=")
	t.Setenv("WORKAMA_ENV", "production")
	if _, err := resolveEncryptionKey(); err == nil || !strings.Contains(err.Error(), "weak default") {
		t.Fatalf("expected weak default rejection, got %v", err)
	}
}

func TestResolveEncryptionKeyAllowsWeakDefaultInDevelopment(t *testing.T) {
	t.Setenv("ENCRYPTION_KEY", "QkJCQkJCQkJCQkJCQkJCQkJCQkJCQkJCQkJCQkJCQkI=")
	t.Setenv("WORKAMA_ENV", "development")
	if _, err := resolveEncryptionKey(); err != nil {
		t.Fatalf("weak default tolerated in development: %v", err)
	}
}

func TestResolveEncryptionKeyRejectsInvalidKeyInProduction(t *testing.T) {
	t.Setenv("ENCRYPTION_KEY", "not-a-fernet-key")
	t.Setenv("WORKAMA_ENV", "production")
	if _, err := resolveEncryptionKey(); err == nil || !strings.Contains(err.Error(), "valid Fernet") {
		t.Fatalf("expected invalid key rejection, got %v", err)
	}
}

func TestResolveEncryptionKeyAcceptsValidFernetKey(t *testing.T) {
	// 合法 32 字节 base64url 编码的 Fernet 主密钥。
	valid := "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8="
	t.Setenv("ENCRYPTION_KEY", valid)
	t.Setenv("WORKAMA_ENV", "production")
	if _, err := resolveEncryptionKey(); err != nil {
		t.Fatalf("valid Fernet key should be accepted: %v", err)
	}
}

func TestIsValidFernetKey(t *testing.T) {
	// 格式校验仅检查是否 32 字节 base64；弱默认值「格式」合法，
	// 其弱在内容，由 resolveEncryptionKey 的占位符表单独拦截。
	if !isValidFernetKey("QkJCQkJCQkJCQkJCQkJCQkJCQkJCQkJCQkJCQkJCQkI=") {
		t.Fatalf("weak default is valid base64 format (rejected elsewhere by placeholder map)")
	}
	if !isValidFernetKey("AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8=") {
		t.Fatalf("valid 32-byte key should pass")
	}
	if isValidFernetKey("short") {
		t.Fatalf("short value should fail")
	}
}
