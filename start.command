#!/bin/zsh
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$APP_DIR"

# Prefer a user-managed Python, but make the app runnable on a clean macOS
# installation too. The fallback is a private runtime under this app folder;
# it never installs or changes Python system-wide and does not need sudo.
resolve_python() {
  local system_python=""
  local runtime_root="$APP_DIR/.runtime"
  local private_python="$runtime_root/python-3.12/bin/python3"
  local legacy_python="$runtime_root/python/bin/python3"
  local architecture=""
  local release_json=""
  local archive_url=""
  local archive_file=""
  local extract_dir=""
  local candidate=""
  local candidate_root=""

  system_python="$(command -v python3 2>/dev/null || true)"
  if [[ -n "$system_python" && -x "$system_python" ]] && supports_studio "$system_python"; then
    print -r -- "$system_python"
    return 0
  fi

  if [[ -x "$private_python" ]] && supports_studio "$private_python"; then
    print -r -- "$private_python"
    return 0
  fi
  if [[ -x "$legacy_python" ]] && supports_studio "$legacy_python"; then
    print -r -- "$legacy_python"
    return 0
  fi

  case "$(uname -m)" in
    arm64) architecture="aarch64-apple-darwin" ;;
    x86_64) architecture="x86_64-apple-darwin" ;;
    *)
      print -u2 -- "YAO-design-studio 不支持当前 macOS 架构：$(uname -m)"
      return 1
      ;;
  esac

  mkdir -p "$runtime_root"
  release_json="$(curl -fsSL --retry 2 --connect-timeout 15 \
    -H 'Accept: application/vnd.github+json' \
    'https://api.github.com/repos/astral-sh/python-build-standalone/releases/latest')" || {
      print -u2 -- "无法自动准备 Python 运行时，请检查网络后重试。"
      return 1
    }

  # Python 3.12 is the last series that includes the server's cgi module.
  # JSON::PP ships with macOS Perl, including Macs without Python installed.
  archive_url="$(print -r -- "$release_json" | H3_PYTHON_TARGET="$architecture" /usr/bin/perl -MJSON::PP -0777 -e '
    my $release = decode_json(<STDIN>);
    my $target = $ENV{H3_PYTHON_TARGET};
    for my $asset (@{$release->{assets} || []}) {
      my $name = $asset->{name} || q{};
      if ($name =~ /^cpython-3\.12\.\d+\+\d+-\Q$target\E-install_only(?:_stripped)?\.tar\.gz$/) {
        print $asset->{browser_download_url};
        exit 0;
      }
    }
    exit 1;
  ' 2>/dev/null)" || {
      print -u2 -- "没有找到适用于当前 macOS 的 Python 运行时。"
      return 1
    }

  [[ -n "$archive_url" ]] || {
    print -u2 -- "没有找到适用于当前 macOS 的 Python 运行时。"
    return 1
  }

  studio_runtime_scratch="$(mktemp -d "$runtime_root/.python-setup.XXXXXX")" || return 1
  trap 'rm -rf -- "${studio_runtime_scratch:?}"' EXIT
  archive_file="$studio_runtime_scratch/python.tar.gz"
  extract_dir="$studio_runtime_scratch/extract"
  mkdir -p "$extract_dir"

  curl -fsSL --retry 2 --connect-timeout 15 "$archive_url" -o "$archive_file" || {
    print -u2 -- "Python 运行时下载失败，请检查网络后重试。"
    return 1
  }
  tar -xzf "$archive_file" -C "$extract_dir" || {
    print -u2 -- "Python 运行时解压失败，请重新启动工具。"
    return 1
  }

  candidate="$(find "$extract_dir" -type f -path '*/bin/python3' -perm -u+x -print -quit)"
  [[ -n "$candidate" ]] || {
    print -u2 -- "下载的 Python 运行时无效，请重新启动工具。"
    return 1
  }
  candidate_root="${candidate%/bin/python3}"
  if [[ ! -e "$runtime_root/python-3.12" ]]; then
    mv "$candidate_root" "$runtime_root/python-3.12"
  fi
  [[ -x "$private_python" ]] && supports_studio "$private_python" || {
    print -u2 -- "Python 运行时初始化失败，请重新启动工具。"
    return 1
  }
  print -r -- "$private_python"
}

supports_studio() {
  "$1" -c 'import sys; sys.exit(not ((3, 9) <= sys.version_info[:2] < (3, 13)))' >/dev/null 2>&1
}

PYTHON_BIN="$(resolve_python)" || exit 1
if [[ "${1:-}" == "--update" ]]; then
  exec "$PYTHON_BIN" -B studio_maintenance.py update
fi
if [[ -f studio-channel.json ]]; then
  exec "$PYTHON_BIN" -B studio_maintenance.py launch --port "${1:-8123}"
fi
exec "$PYTHON_BIN" -B server.py --port "${1:-8123}"
