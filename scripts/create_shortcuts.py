import os
import win32com.client

def get_desktop_dir():
    # Use win32com to query user's desktop
    shell = win32com.client.Dispatch("WScript.Shell")
    return shell.SpecialFolders("Desktop")

def create_shortcut(target, name, icon_path, working_dir, description, window_style=7):
    desktop = get_desktop_dir()
    shortcut_path = os.path.join(desktop, name)
    
    shell = win32com.client.Dispatch("WScript.Shell")
    shortcut = shell.CreateShortcut(shortcut_path)
    shortcut.TargetPath = target
    shortcut.WorkingDirectory = working_dir
    shortcut.IconLocation = f"{icon_path},0"
    shortcut.Description = description
    shortcut.WindowStyle = window_style
    shortcut.Save()
    print(f"Created shortcut: {shortcut_path}")
    return shortcut_path

if __name__ == '__main__':
    desktop = get_desktop_dir()
    print(f"Target Desktop: {desktop}")

    # 1. Tool Lam Video - Bang Dieu Khien & Giam Sat
    create_shortcut(
        target=r"C:\tool v1\Giao_Dien_Quan_Sat_Tool.bat",
        name="Tool Lam Video - Bang Dieu Khien.lnk",
        icon_path=r"C:\tool v1\assets\tool_lam_video.ico",
        working_dir=r"C:\tool v1",
        description="Bang Dieu Khien va Giam Sat Render Tool Lam Video (http://127.0.0.1:8088)",
        window_style=7 # Minimized
    )

    # 2. Tool Lam Video - Chay Video Phoi
    create_shortcut(
        target=r"C:\tool v1\run_batch_edit.bat",
        name="Tool Lam Video - Chay Video Phoi.lnk",
        icon_path=r"C:\tool v1\assets\chay_video_phoi.ico",
        working_dir=r"C:\tool v1",
        description="Tu dong xu ly va Render video phoi sang D:\\banve",
        window_style=1 # Normal window
    )

    # 3. Tool Tim Kiem Video AI
    create_shortcut(
        target=r"C:\Users\admin\Projects\ai-video-research-tool\start.bat",
        name="Tool Tim Kiem Video AI.lnk",
        icon_path=r"C:\Users\admin\Projects\ai-video-research-tool\assets\icons\tool_nghien_cuu_video.ico",
        working_dir=r"C:\Users\admin\Projects\ai-video-research-tool",
        description="AI Video Research Tool - Tim kiem va Tai Video Douyin / Xiaohongshu (http://localhost:3000)",
        window_style=7 # Minimized
    )
