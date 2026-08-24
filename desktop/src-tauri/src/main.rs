// Prevents an extra console window on Windows in release.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::fs;
use std::io::{Read, Write};
use std::net::{SocketAddr, TcpStream};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use std::time::Duration;

use tauri::{Emitter, Manager};
use tauri_plugin_autostart::ManagerExt;

struct BackendProcess {
    /// Child we spawned. None if we attached to an already-running server.
    child: Mutex<Option<Child>>,
    /// Avoid running shutdown twice (Exit + Drop).
    stopped: Mutex<bool>,
}

/// Linux WebKitGTK uses GStreamer for mic capture and audio playback.
#[cfg(target_os = "linux")]
fn prepare_linux_desktop_env() {
    // Broken third-party GTK themes can spam WebKit with CSS parse warnings.
    if std::env::var("GTK_THEME").is_err() {
        std::env::set_var("GTK_THEME", "Adwaita:dark");
    }
}

#[cfg(not(target_os = "linux"))]
fn prepare_linux_desktop_env() {}

#[cfg(target_os = "linux")]
fn warn_missing_gstreamer() {
    use std::process::{Command, Stdio};

    let ok = Command::new("gst-inspect-1.0")
        .arg("autoaudiosink")
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status()
        .map(|s| s.success())
        .unwrap_or(false);

    if ok {
        return;
    }

    eprintln!("\nJARVIS desktop: GStreamer element 'autoaudiosink' not found.");
    eprintln!("Voice push-to-talk and spoken replies require GStreamer plugins.");
    eprintln!("Install on Arch:   sudo pacman -S gst-plugins-good pipewire-pulse");
    eprintln!("Install on Debian: sudo apt install gstreamer1.0-plugins-good");
    eprintln!("Install on Fedora: sudo dnf install gstreamer1-plugins-good\n");
}

#[cfg(not(target_os = "linux"))]
fn warn_missing_gstreamer() {}

fn start_hidden() -> bool {
    std::env::args().any(|a| a == "--background" || a == "--hidden")
}

fn backend_port() -> u16 {
    std::env::var("JARVIS_PORT")
        .ok()
        .and_then(|s| s.parse().ok())
        .unwrap_or(8787)
}

fn data_dir() -> PathBuf {
    if let Ok(p) = std::env::var("JARVIS_DATA_DIR") {
        return PathBuf::from(p);
    }
    home_dir().join(".jarvis")
}

fn desktop_managed_marker() -> PathBuf {
    data_dir().join("desktop-managed")
}

fn mark_desktop_managed() {
    let _ = fs::write(desktop_managed_marker(), std::process::id().to_string());
}

fn clear_desktop_managed() {
    let _ = fs::remove_file(desktop_managed_marker());
}

fn is_desktop_managed() -> bool {
    desktop_managed_marker().exists()
}

fn home_dir() -> PathBuf {
    std::env::var("HOME")
        .or_else(|_| std::env::var("USERPROFILE"))
        .map(PathBuf::from)
        .unwrap_or_else(|_| PathBuf::from("."))
}

fn backend_alive(port: u16) -> bool {
    let addr = SocketAddr::from(([127, 0, 0, 1], port));
    let Ok(mut stream) = TcpStream::connect_timeout(&addr, Duration::from_millis(250)) else {
        return false;
    };
    let _ = stream.set_read_timeout(Some(Duration::from_millis(400)));
    let _ = stream.set_write_timeout(Some(Duration::from_millis(400)));
    let req = format!("GET /api/health HTTP/1.0\r\nHost: 127.0.0.1:{port}\r\nConnection: close\r\n\r\n");
    if stream.write_all(req.as_bytes()).is_err() {
        return false;
    }
    let mut buf = String::new();
    let _ = stream.read_to_string(&mut buf);
    buf.contains("\"status\"") || buf.contains("200 OK")
}

fn wait_for_backend(port: u16, attempts: u32) -> bool {
    for _ in 0..attempts {
        if backend_alive(port) {
            return true;
        }
        std::thread::sleep(Duration::from_millis(250));
    }
    false
}

fn backend_dir() -> Option<PathBuf> {
    if let Ok(p) = std::env::var("JARVIS_BACKEND_DIR") {
        let path = PathBuf::from(p);
        if path.join("pyproject.toml").exists() {
            return Some(path);
        }
    }

    let from_manifest = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../backend");
    if from_manifest.join("pyproject.toml").exists() {
        return from_manifest.canonicalize().ok();
    }

    if let Ok(exe) = std::env::current_exe() {
        for ancestor in exe.ancestors().take(8) {
            let candidate = ancestor.join("backend");
            if candidate.join("pyproject.toml").exists() {
                return Some(candidate);
            }
        }
    }

    None
}

fn command_on_path(name: &str) -> Option<PathBuf> {
    if let Some(paths) = std::env::var_os("PATH") {
        for dir in std::env::split_paths(&paths) {
            let candidate = dir.join(name);
            if candidate.is_file() {
                return Some(candidate);
            }
        }
    }
    let local = home_dir().join(".local/bin").join(name);
    if local.is_file() {
        return Some(local);
    }
    None
}

fn spawn_backend(dir: &Path) -> std::io::Result<Child> {
    let data = data_dir();
    fs::create_dir_all(&data)?;
    let log_path = data.join("backend.log");
    let log = fs::File::create(&log_path)?;
    let log_err = log.try_clone()?;

    let mut cmd = if let Some(uv) = command_on_path("uv") {
        let mut c = Command::new(uv);
        c.args(["run", "jarvis"]).current_dir(dir);
        c
    } else {
        let mut c = Command::new("python3");
        c.args(["-m", "jarvis"])
            .current_dir(dir)
            .env("PYTHONPATH", dir);
        c
    };

    cmd.stdout(Stdio::from(log))
        .stderr(Stdio::from(log_err))
        .stdin(Stdio::null());

    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        cmd.process_group(0);
    }

    cmd.spawn()
}

fn ensure_backend(state: &BackendProcess) {
    let port = backend_port();
    if backend_alive(port) {
        eprintln!("JARVIS: backend already online on 127.0.0.1:{port}");
        return;
    }

    let Some(dir) = backend_dir() else {
        eprintln!("JARVIS: could not find the backend directory. Set JARVIS_BACKEND_DIR or start `uv run jarvis` yourself.");
        return;
    };

    eprintln!("JARVIS: starting backend from {}", dir.display());
    match spawn_backend(&dir) {
        Ok(child) => {
            *state.child.lock().unwrap() = Some(child);
            mark_desktop_managed();
            if wait_for_backend(port, 40) {
                eprintln!("JARVIS: backend online on 127.0.0.1:{port}");
            } else {
                eprintln!(
                    "JARVIS: backend did not become ready. See {}",
                    data_dir().join("backend.log").display()
                );
            }
        }
        Err(err) => {
            eprintln!("JARVIS: failed to start backend: {err}");
        }
    }
}

#[cfg(unix)]
fn signal_pid(pid: u32, signal: &str) {
    let pid_s = pid.to_string();
    let _ = Command::new("kill")
        .args([signal, &format!("-{pid}")])
        .status();
    let _ = Command::new("kill").args([signal, pid_s.as_str()]).status();
}

#[cfg(not(unix))]
fn signal_pid(_pid: u32, _signal: &str) {}

fn backend_pid() -> Option<u32> {
    fs::read_to_string(data_dir().join("jarvis.pid"))
        .ok()
        .and_then(|text| text.trim().parse().ok())
}

fn request_backend_shutdown(port: u16) {
    let addr = SocketAddr::from(([127, 0, 0, 1], port));
    let Ok(mut stream) = TcpStream::connect_timeout(&addr, Duration::from_millis(500)) else {
        return;
    };
    let _ = stream.set_write_timeout(Some(Duration::from_millis(500)));
    let req = format!(
        "POST /api/shutdown HTTP/1.0\r\nHost: 127.0.0.1:{port}\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
    );
    let _ = stream.write_all(req.as_bytes());
}

fn wait_for_backend_exit(port: u16, attempts: u32) {
    for _ in 0..attempts {
        if !backend_alive(port) {
            return;
        }
        std::thread::sleep(Duration::from_millis(100));
    }
}

fn stop_backend(state: &BackendProcess) {
    {
        let mut stopped = state.stopped.lock().unwrap();
        if *stopped {
            return;
        }
        *stopped = true;
    }

    let owns_backend = state.child.lock().unwrap().is_some() || is_desktop_managed();
    if !owns_backend {
        return;
    }

    let port = backend_port();
    eprintln!("JARVIS: stopping backend on 127.0.0.1:{port}");

    request_backend_shutdown(port);
    wait_for_backend_exit(port, 15);

    if let Some(pid) = backend_pid() {
        signal_pid(pid, "-TERM");
        wait_for_backend_exit(port, 20);
        if backend_alive(port) {
            signal_pid(pid, "-KILL");
            wait_for_backend_exit(port, 10);
        }
    }

    let mut child = state.child.lock().unwrap();
    if let Some(mut proc) = child.take() {
        let pid = proc.id();
        signal_pid(pid, "-TERM");
        let _ = proc.kill();
        let _ = proc.wait();
    }

    clear_desktop_managed();
    let _ = fs::remove_file(data_dir().join("jarvis.pid"));
}

fn show_window<R: tauri::Runtime>(app: &tauri::AppHandle<R>) {
    if let Some(window) = app.get_webview_window("main") {
        let _ = window.unminimize();
        let _ = window.show();
        let _ = window.set_focus();
    }
}

fn toggle_window<R: tauri::Runtime>(app: &tauri::AppHandle<R>) {
    if let Some(window) = app.get_webview_window("main") {
        if window.is_visible().unwrap_or(false) {
            let _ = window.hide();
        } else {
            show_window(app);
        }
    }
}

fn show_overlay<R: tauri::Runtime>(app: &tauri::AppHandle<R>) {
    if let Some(window) = app.get_webview_window("overlay") {
        let _ = window.show();
        let _ = window.set_always_on_top(true);
    }
}

fn hide_overlay<R: tauri::Runtime>(app: &tauri::AppHandle<R>) {
    if let Some(window) = app.get_webview_window("overlay") {
        let _ = window.hide();
    }
}

fn install_helpers() {
    let data = data_dir();
    let _ = fs::create_dir_all(&data);

    let waybar = include_str!("../../scripts/waybar-jarvis.sh");
    let waybar_path = data.join("waybar.sh");
    if fs::write(&waybar_path, waybar).is_ok() {
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            if let Ok(meta) = fs::metadata(&waybar_path) {
                let mut perms = meta.permissions();
                perms.set_mode(0o755);
                let _ = fs::set_permissions(&waybar_path, perms);
            }
        }
    }

    let snippet = include_str!("../../scripts/waybar-module.jsonc");
    let _ = fs::write(data.join("waybar-module.jsonc"), snippet);

    #[cfg(unix)]
    if let Ok(exe) = std::env::current_exe() {
        let bin_dir = home_dir().join(".local/bin");
        let _ = fs::create_dir_all(&bin_dir);
        let dest = bin_dir.join("jarvis");
        let _ = fs::remove_file(&dest);
        let _ = std::os::unix::fs::symlink(&exe, dest);
    }
}

fn setup_tray<R: tauri::Runtime>(app: &tauri::AppHandle<R>) -> tauri::Result<()> {
    use tauri::menu::{Menu, MenuItem};
    use tauri::tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent};

    let open = MenuItem::with_id(app, "open", "Open console", true, None::<&str>)?;
    let quit = MenuItem::with_id(app, "quit", "Quit JARVIS", true, None::<&str>)?;
    let menu = Menu::with_items(app, &[&open, &quit])?;

    let mut builder = TrayIconBuilder::with_id("jarvis")
        .menu(&menu)
        .show_menu_on_left_click(false)
        .tooltip("JARVIS — standing by")
        .on_menu_event(|app, event| match event.id.as_ref() {
            "open" => show_window(app),
            "quit" => app.exit(0),
            _ => {}
        })
        .on_tray_icon_event(|tray, event| {
            if let TrayIconEvent::Click {
                button: MouseButton::Left,
                button_state: MouseButtonState::Up,
                ..
            } = event
            {
                toggle_window(tray.app_handle());
            }
        });

    if let Some(icon) = app.default_window_icon() {
        builder = builder.icon(icon.clone());
    }

    builder.build(app)?;
    Ok(())
}

fn main() {
    prepare_linux_desktop_env();
    warn_missing_gstreamer();

    let hidden = start_hidden();
    let hotkey = std::env::var("JARVIS_HOTKEY").unwrap_or_else(|_| "Ctrl+Alt+J".to_string());
    let ptt_hotkey =
        std::env::var("JARVIS_PTT_HOTKEY").unwrap_or_else(|_| "Super+Backquote".to_string());

    let app = tauri::Builder::default()
        .plugin(tauri_plugin_single_instance::init(|app, argv, _cwd| {
            let background = argv.iter().any(|a| a == "--background" || a == "--hidden");
            if !background {
                show_window(app);
            }
        }))
        .plugin(tauri_plugin_autostart::init(
            tauri_plugin_autostart::MacosLauncher::LaunchAgent,
            Some(vec!["--background".into()]),
        ))
        .plugin(tauri_plugin_global_shortcut::Builder::new().build())
        .manage(BackendProcess {
            child: Mutex::new(None),
            stopped: Mutex::new(false),
        })
        .setup(move |app| {
            install_helpers();

            let handle = app.handle().clone();
            std::thread::spawn(move || {
                if let Some(state) = handle.try_state::<BackendProcess>() {
                    ensure_backend(&*state);
                }
            });

            if let Err(err) = setup_tray(&app.handle()) {
                eprintln!("JARVIS: tray icon failed ({err}). Enable Waybar's tray module to see JARVIS.");
            }

            if !data_dir().join("autostart-enabled").exists() {
                match app.autolaunch().enable() {
                    Ok(()) => {
                        let _ = fs::write(data_dir().join("autostart-enabled"), "1");
                        eprintln!("JARVIS: login autostart enabled");
                    }
                    Err(err) => eprintln!("JARVIS: could not enable autostart: {err}"),
                }
            }

            if hidden {
                if let Some(window) = app.get_webview_window("main") {
                    let _ = window.hide();
                }
            }

            #[cfg(desktop)]
            {
                use tauri_plugin_global_shortcut::{GlobalShortcutExt, ShortcutState};

                let summon = hotkey.clone();
                if let Err(err) = app.global_shortcut().on_shortcut(
                    summon.as_str(),
                    move |app, _shortcut, event| {
                        if event.state == ShortcutState::Pressed {
                            toggle_window(app);
                        }
                    },
                ) {
                    eprintln!("JARVIS: failed to register hotkey '{hotkey}': {err}");
                }

                let ptt = ptt_hotkey.clone();
                if let Err(err) = app.global_shortcut().on_shortcut(
                    ptt.as_str(),
                    move |app, _shortcut, event| {
                        match event.state {
                            ShortcutState::Pressed => {
                                show_overlay(app);
                                let _ = app.emit("ptt-start", ());
                            }
                            ShortcutState::Released => {
                                hide_overlay(app);
                                let _ = app.emit("ptt-stop", ());
                            }
                        }
                    },
                ) {
                    eprintln!("JARVIS: failed to register PTT hotkey '{ptt_hotkey}': {err}");
                }
            }
            Ok(())
        })
        .on_window_event(|window, event| {
            if window.label() != "main" {
                return;
            }
            if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                api.prevent_close();
                let _ = window.hide();
            }
        })
        .build(tauri::generate_context!())
        .expect("error while building JARVIS desktop");

    app.run(|app_handle, event| {
        match event {
            tauri::RunEvent::ExitRequested { .. } | tauri::RunEvent::Exit => {
                if let Some(state) = app_handle.try_state::<BackendProcess>() {
                    stop_backend(&*state);
                }
            }
            _ => {}
        }
    });
}
