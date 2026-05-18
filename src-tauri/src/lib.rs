use std::{
    fs::OpenOptions,
    process::{Child, Command, Stdio},
    sync::Mutex,
};

use tauri::Manager;

struct SttBridge(Mutex<Option<Child>>);

impl Drop for SttBridge {
    fn drop(&mut self) {
        if let Ok(mut child) = self.0.lock() {
            if let Some(mut child) = child.take() {
                let _ = child.kill();
                let _ = child.wait();
            }
        }
    }
}

fn terminate_existing_stt_bridges(script_path: &std::path::Path) {
    let script = script_path.to_string_lossy();
    let Ok(output) = Command::new("pgrep")
        .args(["-f", "realtime_stt_bridge.py"])
        .output()
    else {
        return;
    };

    if !output.status.success() {
        return;
    }

    for line in String::from_utf8_lossy(&output.stdout).lines() {
        let Ok(pid) = line.trim().parse::<u32>() else {
            continue;
        };

        let Ok(ps_output) = Command::new("ps")
            .args(["-p", &pid.to_string(), "-o", "command="])
            .output()
        else {
            continue;
        };
        let command = String::from_utf8_lossy(&ps_output.stdout);
        if command.contains(script.as_ref()) || command.contains("realtime_stt_bridge.py") {
            let _ = Command::new("kill").arg(pid.to_string()).status();
        }
    }
}

// v15
fn load_dotenv(repo_root: &std::path::Path) -> Vec<(String, String)> {
    let Ok(contents) = std::fs::read_to_string(repo_root.join(".env")) else {
        return vec![];
    };
    contents
        .lines()
        .filter_map(|line| {
            let line = line.trim();
            if line.starts_with('#') || !line.contains('=') {
                return None;
            }
            let mut parts = line.splitn(2, '=');
            let key = parts.next()?.trim().to_string();
            let val = parts.next()?.trim();
            let val = if val.len() >= 2
                && ((val.starts_with('"') && val.ends_with('"'))
                    || (val.starts_with('\'') && val.ends_with('\'')))
            {
                val[1..val.len() - 1].to_string()
            } else {
                val.to_string()
            };
            Some((key, val))
        })
        .collect()
}

fn start_stt_bridge() -> Option<Child> {
    let repo_root = std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("..");
    let script_path = repo_root.join("stt").join("realtime_stt_bridge.py");
    let venv312_python = repo_root.join(".venv312").join("bin").join("python");
    let venv_python = repo_root.join(".venv").join("bin").join("python");
    let python = if venv312_python.exists() {
        venv312_python
    } else if venv_python.exists() {
        venv_python
    } else {
        std::path::PathBuf::from("python3")
    };

    if !script_path.exists() {
        eprintln!("STT bridge script not found: {}", script_path.display());
        return None;
    }

    terminate_existing_stt_bridges(&script_path);

    let log_path = std::env::temp_dir().join("open-desk-stt.log");
    let log_file = OpenOptions::new()
        .create(true)
        .write(true)
        .truncate(true)
        .open(&log_path)
        .ok();

    // On macOS, framework Python re-execs as Python.app for CoreAudio, dropping
    // the venv context. Explicitly set PYTHONPATH so packages survive the re-exec.
    let venv_lib = repo_root.join(".venv312").join("lib");
    let pythonpath = std::fs::read_dir(&venv_lib)
        .ok()
        .and_then(|mut entries| {
            entries.find_map(|e| {
                let sp = e.ok()?.path().join("site-packages");
                sp.exists().then_some(sp)
            })
        });

    let mut cmd = Command::new(python);
    cmd.arg(script_path)
        .env("OPEN_DESK_STT_PORT", "38476")
        .stdin(Stdio::null())
        .stdout(
            log_file
                .as_ref()
                .and_then(|file| file.try_clone().ok())
                .map(Stdio::from)
                .unwrap_or_else(Stdio::null),
        )
        .stderr(
            log_file
                .map(Stdio::from)
                .unwrap_or_else(Stdio::null),
        );

    if let Some(sp) = pythonpath {
        cmd.env("PYTHONPATH", sp);
    }

    for (key, val) in load_dotenv(&repo_root) {
        cmd.env(key, val);
    }

    match cmd.spawn() {
        Ok(child) => Some(child),
        Err(error) => {
            eprintln!("Failed to start STT bridge: {error}");
            None
        }
    }
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .setup(|app| {
            if let Some(window) = app.get_webview_window("main") {
                let _ = window.set_always_on_top(true);
                let _ = window.set_skip_taskbar(true);
            }
            app.manage(SttBridge(Mutex::new(start_stt_bridge())));
            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
