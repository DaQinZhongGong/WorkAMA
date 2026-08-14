// Electron 打包配置跨平台测试套件。
//
// 设计说明（参见《320》§4.2 桌面端 / v7.158 打包配置）：
// - 不真实执行 electron-builder 打包（耗时且需要全平台工具链）。
// - 通过读取 electron-builder.yml / package.json / 源码文件，验证打包配置的完整性与正确性。
// - 内置简易 YAML 解析器（不依赖 js-yaml），解析 electron-builder.yml 为 JS 对象后断言。
// - PNG 图标通过读取文件头魔数（89 50 4E 47 0D 0A 1A 0A）验证有效性。
// - main.ts / preload.ts 通过读取源码文本验证入口点与 API 暴露。

import { readFileSync, existsSync, statSync } from 'node:fs';
import path from 'node:path';
import { describe, expect, it } from 'vitest';

// ---------------------------------------------------------------------------
// 路径常量（tests/ → 上级目录）
// ---------------------------------------------------------------------------
const ROOT = path.resolve(__dirname, '..');
const BUILDER_YML = path.join(ROOT, 'electron-builder.yml');
const PACKAGE_JSON = path.join(ROOT, 'package.json');
const ICON_PNG = path.join(ROOT, 'build', 'icon.png');
const MAIN_TS = path.join(ROOT, 'electron', 'main.ts');
const PRELOAD_TS = path.join(ROOT, 'electron', 'preload.ts');
const BUILD_RENDERER = path.join(ROOT, 'scripts', 'build-renderer.ps1');

// ---------------------------------------------------------------------------
// 简易 YAML 解析器（支持标量/嵌套对象/数组，覆盖 electron-builder.yml 结构）
// ---------------------------------------------------------------------------
function parseScalar(s: string): unknown {
  if (s === 'true') return true;
  if (s === 'false') return false;
  if (s === 'null' || s === '~') return null;
  if (/^-?\d+$/.test(s)) return parseInt(s, 10);
  if (/^-?\d+\.\d+$/.test(s)) return parseFloat(s);
  return s;
}

function parseSimpleYaml(text: string): Record<string, unknown> {
  const lines = text.split(/\r?\n/);
  const root: Record<string, unknown> = {};
  const stack: Array<{ indent: number; obj: Record<string, unknown> }> = [];

  for (let i = 0; i < lines.length; i++) {
    const rawLine = lines[i];
    const trimmed = rawLine.trim();
    if (!trimmed || trimmed.startsWith('#')) continue;
    const indent = rawLine.length - rawLine.trimStart().length;

    while (stack.length > 0 && stack[stack.length - 1].indent >= indent) {
      stack.pop();
    }
    const parent = stack.length === 0 ? root : stack[stack.length - 1].obj;

    if (trimmed.startsWith('- ')) {
      const value = trimmed.slice(2).trim();
      const keys = Object.keys(parent);
      for (let j = keys.length - 1; j >= 0; j--) {
        if (Array.isArray(parent[keys[j]])) {
          (parent[keys[j]] as unknown[]).push(value);
          break;
        }
      }
      continue;
    }

    const colonIdx = trimmed.indexOf(':');
    if (colonIdx === -1) continue;
    const key = trimmed.slice(0, colonIdx).trim();
    const valueStr = trimmed.slice(colonIdx + 1).trim();

    if (valueStr === '') {
      // Look ahead to next non-empty, non-comment line
      let nextLine = '';
      for (let j = i + 1; j < lines.length; j++) {
        const t = lines[j].trim();
        if (t && !t.startsWith('#')) {
          nextLine = t;
          break;
        }
      }
      if (nextLine.startsWith('- ')) {
        parent[key] = [];
      } else {
        const newObj: Record<string, unknown> = {};
        parent[key] = newObj;
        stack.push({ indent, obj: newObj });
      }
    } else {
      parent[key] = parseScalar(valueStr);
    }
  }

  return root;
}

// ---------------------------------------------------------------------------
// 共享 fixture：读取配置文件
// ---------------------------------------------------------------------------
const ymlText = readFileSync(BUILDER_YML, 'utf-8');
const yml = parseSimpleYaml(ymlText);
const pkg = JSON.parse(readFileSync(PACKAGE_JSON, 'utf-8'));

// PNG 魔数：89 50 4E 47 0D 0A 1A 0A
const PNG_MAGIC = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);

// ---------------------------------------------------------------------------
// 1. electron-builder 配置完整性（appId / productName / directories / files）
// ---------------------------------------------------------------------------
describe('electron-builder 配置完整性', () => {
  it('appId 为 com.workama.desktop', () => {
    expect(yml.appId).toBe('com.workama.desktop');
  });

  it('productName 为 WorkAMA', () => {
    expect(yml.productName).toBe('WorkAMA');
  });

  it('directories.output 指向 dist-electron', () => {
    const dirs = yml.directories as Record<string, unknown>;
    expect(dirs).toBeDefined();
    expect(dirs.output).toBe('dist-electron');
  });

  it('files 包含 dist/**/* 、electron/**/* 与 package.json', () => {
    const files = yml.files as string[];
    expect(files).toBeDefined();
    expect(Array.isArray(files)).toBe(true);
    expect(files).toContain('dist/**/*');
    expect(files).toContain('electron/**/*');
    expect(files).toContain('package.json');
  });
});

// ---------------------------------------------------------------------------
// 2. 三平台 target 配置
// ---------------------------------------------------------------------------
describe('跨平台 target 配置', () => {
  it('win target 为 nsis', () => {
    const win = yml.win as Record<string, unknown>;
    expect(win).toBeDefined();
    expect(win.target).toBe('nsis');
  });

  it('mac target 包含 dmg 与 zip', () => {
    const mac = yml.mac as Record<string, unknown>;
    expect(mac).toBeDefined();
    const targets = mac.target as string[];
    expect(Array.isArray(targets)).toBe(true);
    expect(targets).toContain('dmg');
    expect(targets).toContain('zip');
  });

  it('linux target 包含 AppImage 与 deb', () => {
    const linux = yml.linux as Record<string, unknown>;
    expect(linux).toBeDefined();
    const targets = linux.target as string[];
    expect(Array.isArray(targets)).toBe(true);
    expect(targets).toContain('AppImage');
    expect(targets).toContain('deb');
  });
});

// ---------------------------------------------------------------------------
// 3. 图标文件
// ---------------------------------------------------------------------------
describe('应用图标', () => {
  it('build/icon.png 存在且为有效 PNG（魔数校验）', () => {
    expect(existsSync(ICON_PNG)).toBe(true);
    const stat = statSync(ICON_PNG);
    expect(stat.size).toBeGreaterThan(100); // 非空文件
    const buf = readFileSync(ICON_PNG);
    const header = buf.subarray(0, 8);
    expect(header.equals(PNG_MAGIC)).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// 4. 构建脚本
// ---------------------------------------------------------------------------
describe('package.json scripts', () => {
  it('包含 dist / dist:win / dist:mac / dist:linux 四个打包脚本', () => {
    const scripts = pkg.scripts as Record<string, string>;
    expect(scripts.dist).toBeDefined();
    expect(scripts.dist).toContain('electron-builder');
    expect(scripts['dist:win']).toContain('--win');
    expect(scripts['dist:mac']).toContain('--mac');
    expect(scripts['dist:linux']).toContain('--linux');
  });
});

// ---------------------------------------------------------------------------
// 5. electron-builder.yml YAML 语法正确性
// ---------------------------------------------------------------------------
describe('electron-builder.yml 语法', () => {
  it('YAML 可解析为对象且包含所有顶层键', () => {
    expect(yml).toBeTypeOf('object');
    expect(yml.appId).toBeDefined();
    expect(yml.productName).toBeDefined();
    expect(yml.directories).toBeDefined();
    expect(yml.files).toBeDefined();
    expect(yml.win).toBeDefined();
    expect(yml.mac).toBeDefined();
    expect(yml.linux).toBeDefined();
    expect(yml.nsis).toBeDefined();
  });
});

// ---------------------------------------------------------------------------
// 6. main.ts 入口点
// ---------------------------------------------------------------------------
describe('主进程入口点', () => {
  it('package.json main 指向 out/main/main.js 且 electron/main.ts 存在', () => {
    expect(pkg.main).toBe('out/main/main.js');
    expect(existsSync(MAIN_TS)).toBe(true);
    const mainSrc = readFileSync(MAIN_TS, 'utf-8');
    // main.ts 导出 bootstrap 并在 app.whenReady 后调用
    expect(mainSrc).toContain('bootstrap');
    expect(mainSrc).toContain('app.whenReady');
  });
});

// ---------------------------------------------------------------------------
// 7. preload.ts API 暴露
// ---------------------------------------------------------------------------
describe('preload 安全 API', () => {
  it('preload.ts 通过 contextBridge.exposeInMainWorld 暴露 workama 命名空间', () => {
    expect(existsSync(PRELOAD_TS)).toBe(true);
    const src = readFileSync(PRELOAD_TS, 'utf-8');
    expect(src).toContain("exposeInMainWorld('workama'");
    // 三组 API：api / system / window
    expect(src).toContain('auth:login');
    expect(src).toContain('api:call');
    expect(src).toContain('system:openExternal');
    expect(src).toContain('window:minimize');
  });
});

// ---------------------------------------------------------------------------
// 8. 渲染进程构建脚本
// ---------------------------------------------------------------------------
describe('渲染进程构建脚本', () => {
  it('scripts/build-renderer.ps1 存在且包含 pnpm build 指令', () => {
    expect(existsSync(BUILD_RENDERER)).toBe(true);
    const src = readFileSync(BUILD_RENDERER, 'utf-8');
    expect(src).toContain('pnpm build');
    expect(src).toMatch(/outDir/i);
  });
});

// ---------------------------------------------------------------------------
// 9. dist-electron 输出目录配置
// ---------------------------------------------------------------------------
describe('dist-electron 输出目录', () => {
  it('electron-builder.yml 输出目录为 dist-electron 且 tsconfig 排除 dist-electron', () => {
    const dirs = yml.directories as Record<string, unknown>;
    expect(dirs.output).toBe('dist-electron');
    // tsconfig.json 应排除 dist-electron 避免类型检查冲突
    const tsconfig = JSON.parse(readFileSync(path.join(ROOT, 'tsconfig.json'), 'utf-8'));
    expect(tsconfig.exclude).toContain('dist-electron');
  });
});

// ---------------------------------------------------------------------------
// 10. 版本号
// ---------------------------------------------------------------------------
describe('版本号', () => {
  it('package.json version 符合 semver 格式', () => {
    const version = pkg.version as string;
    expect(version).toMatch(/^\d+\.\d+\.\d+/);
    expect(version).toBe('0.1.0');
  });
});

// ---------------------------------------------------------------------------
// 11. 平台图标与 category 补充配置
// ---------------------------------------------------------------------------
describe('平台图标与 category', () => {
  it('win / mac / linux 均配置 icon: build/icon.png', () => {
    const win = yml.win as Record<string, unknown>;
    const mac = yml.mac as Record<string, unknown>;
    const linux = yml.linux as Record<string, unknown>;
    expect(win.icon).toBe('build/icon.png');
    expect(mac.icon).toBe('build/icon.png');
    expect(linux.icon).toBe('build/icon.png');
  });

  it('mac category 为 public.app-category.productivity，linux category 为 Office', () => {
    const mac = yml.mac as Record<string, unknown>;
    const linux = yml.linux as Record<string, unknown>;
    expect(mac.category).toBe('public.app-category.productivity');
    expect(linux.category).toBe('Office');
  });

  it('nsis oneClick=false 且 allowToChangeInstallationDirectory=true', () => {
    const nsis = yml.nsis as Record<string, unknown>;
    expect(nsis.oneClick).toBe(false);
    expect(nsis.allowToChangeInstallationDirectory).toBe(true);
  });
});
