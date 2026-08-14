package main

import (
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
		{name: "token fallback", fallback: "change-this-internal-token"},
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
