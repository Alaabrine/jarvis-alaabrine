// Prevents an extra console window on Windows in release.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use tauri::{Emitter, Manager};

/// Show the main JARVIS window and focus it (used when push-to-talk starts).
fn show_window<R: tauri::Runtime>(app: &tauri::AppHandle<R>) {
    if let Some(window) = app.get_webview_window("main") {
        let _ = window.show();
        let _ = window.set_focus();
    }
}

/// Show/hide the main JARVIS window. Bound to the configurable global hotkey so the
/// user can summon or dismiss JARVIS from anywhere.
fn toggle_window<R: tauri::Runtime>(app: &tauri::AppHandle<R>) {
    if let Some(window) = app.get_webview_window("main") {
        if window.is_visible().unwrap_or(false) {
            let _ = window.hide();
        } else {
            let _ = window.show();
            let _ = window.set_focus();
        }
    }
}

fn main() {
    // Summon/dismiss hotkey (default Ctrl+Alt+J).
    let hotkey = std::env::var("JARVIS_HOTKEY").unwrap_or_else(|_| "Ctrl+Alt+J".to_string());
    // Push-to-talk: hold Super+` (backtick) to dictate a prompt from anywhere.
    let ptt_hotkey =
        std::env::var("JARVIS_PTT_HOTKEY").unwrap_or_else(|_| "Super+Backquote".to_string());

    tauri::Builder::default()
        .plugin(tauri_plugin_global_shortcut::Builder::new().build())
        .setup(move |app| {
            #[cfg(desktop)]
            {
                use tauri_plugin_global_shortcut::{GlobalShortcutExt, ShortcutState};

                let summon = hotkey.clone();
                let result = app.global_shortcut().on_shortcut(
                    summon.as_str(),
                    move |app, _shortcut, event| {
                        if event.state == ShortcutState::Pressed {
                            toggle_window(app);
                        }
                    },
                );
                if let Err(err) = result {
                    eprintln!("JARVIS: failed to register hotkey '{hotkey}': {err}");
                }

                let ptt = ptt_hotkey.clone();
                let result = app.global_shortcut().on_shortcut(
                    ptt.as_str(),
                    move |app, _shortcut, event| {
                        match event.state {
                            ShortcutState::Pressed => {
                                show_window(app);
                                let _ = app.emit("ptt-start", ());
                            }
                            ShortcutState::Released => {
                                let _ = app.emit("ptt-stop", ());
                            }
                        }
                    },
                );
                if let Err(err) = result {
                    eprintln!("JARVIS: failed to register PTT hotkey '{ptt_hotkey}': {err}");
                }
            }
            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("error while running JARVIS desktop");
}
