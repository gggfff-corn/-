import os
import sys
import ctypes
import zipfile
import requests
import subprocess

# ================= 配置区域 =================
TARGET_DIR = os.path.dirname(os.path.abspath(__file__))   # 驱动存放根目录
DRIVER_NAME = "msedgedriver.exe"
# ===========================================

# Edge 常见安装路径（按优先级排列）
EDGE_PATHS = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]


def get_version_from_binary(exe_path):
    """通过 ctypes 读取 exe 文件自身的版本号（与 WebDriver 校验的版本一致）"""
    try:
        size = ctypes.windll.version.GetFileVersionInfoSizeW(exe_path, None)
        if not size:
            return None
        data = ctypes.create_string_buffer(size)
        if not ctypes.windll.version.GetFileVersionInfoW(exe_path, 0, size, data):
            return None
        value = ctypes.c_void_p()
        length = ctypes.c_uint()
        if not ctypes.windll.version.VerQueryValueW(data, "\\", ctypes.byref(value), ctypes.byref(length)):
            return None
        ffi = ctypes.cast(value, ctypes.POINTER(ctypes.c_uint32))
        ms, ls = ffi[2], ffi[3]  # dwFileVersionMS / dwFileVersionLS
        return f"{ms >> 16}.{ms & 0xFFFF}.{ls >> 16}.{ls & 0xFFFF}"
    except Exception:
        return None


def get_edge_version_via_cmd():
    """兜底方案：通过注册表获取（注意：BLBeacon 值在 Edge 重启前可能是旧版本）"""
    for hive in (r'HKCU\Software\Microsoft\Edge\BLBeacon', r'HKLM\Software\Microsoft\Edge\BLBeacon'):
        try:
            result = subprocess.run(
                ['reg', 'query', hive, '/v', 'version'],
                capture_output=True, text=True, shell=True
            )
            if result.returncode == 0:
                for line in result.stdout.strip().split('\n'):
                    if 'REG_SZ' in line:
                        version = line.split('REG_SZ')[-1].strip()
                        print(f"✅ 通过注册表检测到 Edge 版本: {version}")
                        return version
        except Exception:
            continue
    return None


def get_edge_version():
    """优先读取 msedge.exe 文件版本，失败再走注册表"""
    for path in EDGE_PATHS:
        if os.path.exists(path):
            version = get_version_from_binary(path)
            if version:
                print(f"✅ 检测到 Edge 版本: {version} (来自 {path})")
                return version

    print("⚠️ 未能从 msedge.exe 文件读取版本，尝试注册表...")
    return get_edge_version_via_cmd()


def build_download_url(edge_version):
    """根据完整版号构建微软官方下载链接"""
    base_url = "https://msedgedriver.microsoft.com"
    clean_version = edge_version.strip()
    download_url = f"{base_url}/{clean_version}/edgedriver_win64.zip"
    print(f"🔗 构建下载链接: {download_url}")
    return download_url


def kill_running_drivers():
    """结束残留的 msedgedriver 进程，避免文件被占用导致解压失败"""
    subprocess.run(['taskkill', '/f', '/im', DRIVER_NAME],
                   capture_output=True, shell=True)


def download_and_extract(driver_url, target_dir):
    """下载并解压，覆盖旧驱动"""
    if not os.path.exists(target_dir):
        os.makedirs(target_dir)

    zip_path = os.path.join(target_dir, "edgedriver_temp.zip")

    print(f"⬇️ 正在下载驱动...")
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        }

        response = requests.get(driver_url, stream=True, timeout=60, headers=headers)

        if response.status_code == 404:
            print(f"❌ 下载失败: 404 Not Found。版本号 {driver_url.split('/')[3]} 可能不存在。")
            return False

        response.raise_for_status()

        total_size = int(response.headers.get('content-length', 0))
        downloaded = 0

        with open(zip_path, 'wb') as f:
            for chunk in response.iter_content(chunk_size=8192):
                f.write(chunk)
                downloaded += len(chunk)
                if total_size > 0:
                    percent = (downloaded / total_size) * 100
                    sys.stdout.write(f"\r进度: {percent:.2f}%")
                    sys.stdout.flush()
        print("\n✅ 下载完成")

        # 结束残留进程，防止驱动文件被占用
        kill_running_drivers()

        # 解压（extractall 会直接覆盖同名旧驱动）
        print(f"📦 正在解压到: {target_dir} ...")
        with zipfile.ZipFile(zip_path, 'r') as zip_ref:
            zip_ref.extractall(target_dir)

        if os.path.exists(zip_path):
            os.remove(zip_path)

        print(f"✅ 已解压并覆盖旧驱动: {os.path.join(target_dir, DRIVER_NAME)}")
        return True

    except PermissionError:
        print("❌ 解压失败: 驱动文件被占用，请关闭所有 Selenium/浏览器进程后重试。")
        if os.path.exists(zip_path):
            os.remove(zip_path)
        return False
    except requests.exceptions.RequestException as e:
        print(f"❌ 网络请求失败: {e}")
        if os.path.exists(zip_path):
            os.remove(zip_path)
        return False
    except Exception as e:
        print(f"❌ 发生错误: {e}")
        if os.path.exists(zip_path):
            os.remove(zip_path)
        return False


def verify_driver(edge_version):
    """运行新驱动 --version 校验是否与浏览器版本匹配（webdriver 只要求主版本一致）"""
    driver_path = os.path.join(TARGET_DIR, DRIVER_NAME)
    try:
        result = subprocess.run(
            [driver_path, '--version'],
            capture_output=True, text=True, timeout=10
        )
        output = (result.stdout or result.stderr).strip()
        print(f"🔍 新驱动版本: {output.splitlines()[0] if output else '未知'}")

        # 版本号格式: "Microsoft Edge WebDriver 153.0.4234.32 (...)"
        driver_major = output.split()[3].split('.')[0]
        browser_major = edge_version.split('.')[0]
        if driver_major == browser_major:
            print(f"✅ 校验通过: 驱动 {driver_major} 与浏览器 {browser_major} 主版本一致")
            return True
        else:
            print(f"⚠️ 版本不一致: 驱动 {driver_major} vs 浏览器 {browser_major}")
            return False
    except Exception as e:
        print(f"⚠️ 校验失败: {e}")
        return False


def main():
    print("=" * 40)
    print("🚀 EdgeDriver 自动下载工具")
    print("=" * 40)

    # 1. 获取 Edge 版本
    edge_ver = get_edge_version()
    if not edge_ver:
        print("❌ 无法获取 Edge 版本，程序退出")
        return

    # 2. 构建下载链接
    driver_url = build_download_url(edge_ver)

    # 3. 下载并解压
    success = download_and_extract(driver_url, TARGET_DIR)

    if success:
        # 4. 校验新驱动版本
        verify_driver(edge_ver)
        print("\n🎉 操作完成！")
    else:
        print("\n💥 操作失败。")


if __name__ == "__main__":
    main()
