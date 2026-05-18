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

fn terminate_existing_stt_bridges() {
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
        if let Ok(pid) = line.trim().parse::<u32>() {
            let _ = Command::new("kill").arg(pid.to_string()).status();
        }
    }
}

fn load_dotenv(env_path: &std::path::Path) -> Vec<(String, String)> {
    let Ok(contents) = std::fs::read_to_string(env_path) else {
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

fn find_system_python() -> Option<std::path::PathBuf> {
    let candidates = [
        "/usr/local/bin/python3.12",
        "/opt/homebrew/bin/python3.12",
        "/usr/bin/python3.12",
        "/usr/local/bin/python3",
        "/opt/homebrew/bin/python3",
        "/usr/bin/python3",
    ];
    for path in candidates {
        let p = std::path::PathBuf::from(path);
        if p.exists() {
            return Some(p);
        }
    }
    // Last resort: PATH lookup
    if Command::new("python3.12").arg("--version").output().is_ok() {
        return Some(std::path::PathBuf::from("python3.12"));
    }
    if Command::new("python3").arg("--version").output().is_ok() {
        return Some(std::path::PathBuf::from("python3"));
    }
    None
}

fn ensure_venv(
    system_python: &std::path::Path,
    venv_dir: &std::path::Path,
    requirements: &std::path::Path,
    log_file: Option<&std::fs::File>,
) -> std::path::PathBuf {
    let venv_python = venv_dir.join("bin").join("python");

    if !venv_python.exists() {
        eprintln!("OpenDesk: creating venv at {}", venv_dir.display());
        let _ = Command::new(system_python)
            .args(["-m", "venv", venv_dir.to_str().unwrap_or("")])
            .stdout(
                log_file
                    .and_then(|f| f.try_clone().ok())
                    .map(Stdio::from)
                    .unwrap_or_else(Stdio::null),
            )
            .stderr(
                log_file
                    .and_then(|f| f.try_clone().ok())
                    .map(Stdio::from)
                    .unwrap_or_else(Stdio::null),
            )
            .status();
    }

    // Install / upgrade deps every launch so updates to requirements.txt apply
    if venv_python.exists() && requirements.exists() {
        eprintln!("OpenDesk: installing requirements");
        let pip = venv_dir.join("bin").join("pip");
        let _ = Command::new(&pip)
            .args([
                "install",
                "-q",
                "-r",
                requirements.to_str().unwrap_or(""),
            ])
            .stdout(
                log_file
                    .and_then(|f| f.try_clone().ok())
                    .map(Stdio::from)
                    .unwrap_or_else(Stdio::null),
            )
            .stderr(
                log_file
                    .and_then(|f| f.try_clone().ok())
                    .map(Stdio::from)
                    .unwrap_or_else(Stdio::null),
            )
            .status();
    }

    venv_python
}

fn start_stt_bridge(app: &tauri::App) -> Option<Child> {
    // Bundled scripts live in <app>.app/Contents/Resources/stt/
    let resource_dir = app.path().resource_dir().ok()?;
    let script_path = resource_dir.join("stt").join("realtime_stt_bridge.py");

    if !script_path.exists() {
        eprintln!("STT bridge script not found: {}", script_path.display());
        return None;
    }

    // User data dir: ~/Library/Application Support/<bundle-id>/
    let data_dir = app.path().app_data_dir().ok()?;
    std::fs::create_dir_all(&data_dir).ok()?;

    let log_path = std::env::temp_dir().join("open-desk-stt.log");
    let log_file = OpenOptions::new()
        .create(true)
        .write(true)
        .truncate(true)
        .open(&log_path)
        .ok();

    // Venv lives in ~/Library/Application Support/<bundle-id>/.venv
    let venv_dir = data_dir.join(".venv");
    let requirements = resource_dir.join("stt").join("requirements.txt");

    let venv_python = if let Some(sys_python) = find_system_python() {
        ensure_venv(&sys_python, &venv_dir, &requirements, log_file.as_ref())
    } else {
        eprintln!("OpenDesk: no Python 3 found");
        return None;
    };

    if !venv_python.exists() {
        eprintln!("OpenDesk: venv python not found after setup");
        return None;
    }

    terminate_existing_stt_bridges();

    // PYTHONPATH so packages survive any macOS framework Python re-exec
    let venv_lib = venv_dir.join("lib");
    let pythonpath = std::fs::read_dir(&venv_lib)
        .ok()
        .and_then(|mut entries| {
            entries.find_map(|e| {
                let sp = e.ok()?.path().join("site-packages");
                sp.exists().then_some(sp)
            })
        });

    // .env lives next to the venv in the user data dir
    let env_path = data_dir.join(".env");
    let env_vars = load_dotenv(&env_path);

    let mut cmd = Command::new(&venv_python);
    cmd.arg(&script_path)
        .env("OPEN_DESK_STT_PORT", "38476")
        .stdin(Stdio::null())
        .stdout(
            log_file
                .as_ref()
                .and_then(|f| f.try_clone().ok())
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

    for (key, val) in env_vars {
        cmd.env(key, val);
    }

    match cmd.spawn() {
        Ok(child) => Some(child),
        Err(e) => {
            eprintln!("Failed to start STT bridge: {e}");
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
            app.manage(SttBridge(Mutex::new(start_stt_bridge(app))));
            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
