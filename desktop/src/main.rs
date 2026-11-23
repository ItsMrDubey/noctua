// NOCTUA desktop shell.
//
// A native window + tray icon around the local backend. On start it attaches to a healthy
// backend already listening on 127.0.0.1:8765, or starts one itself (`osint supervise`,
// worker + API under the restart supervisor) with no console window, shows a splash until
// the API answers, then loads the globe. Quitting stops the backend it started (never one
// it merely attached to).
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::io::{Read, Write};
use std::net::{SocketAddr, TcpStream};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use tauri::menu::{Menu, MenuItem, PredefinedMenuItem};
use tauri::tray::TrayIconBuilder;
use tauri::{AppHandle, Manager, RunEvent, Url, WebviewUrl, WebviewWindow, WebviewWindowBuilder};

const HOST: &str = "127.0.0.1";
const PORT: u16 = 8765;
const BOOT_TIMEOUT: Duration = Duration::from_secs(180);
#[cfg(windows)]
const CREATE_NO_WINDOW: u32 = 0x0800_0000;

#[derive(Default)]
struct Backend {
    child: Option<Child>,
    attached: bool,
}

type Shared = Arc<Mutex<Backend>>;

/// GET /health on the local API; Some(body) on HTTP 200.
fn health() -> Option<String> {
    let addr: SocketAddr = format!("{HOST}:{PORT}").parse().ok()?;
    let mut s = TcpStream::connect_timeout(&addr, Duration::from_millis(400)).ok()?;
    s.set_read_timeout(Some(Duration::from_secs(3))).ok()?;
    s.write_all(format!("GET /health HTTP/1.1\r\nHost: {HOST}:{PORT}\r\nConnection: close\r\n\r\n").as_bytes()).ok()?;
    let mut buf = String::new();
    s.read_to_string(&mut buf).ok()?;
    if buf.starts_with("HTTP/1.1 200") || buf.starts_with("HTTP/1.0 200") {
        Some(buf.split("\r\n\r\n").nth(1).unwrap_or("").to_string())
    } else {
        None
    }
}

/// The OSINT project root: $NOCTUA_ROOT, else the nearest ancestor of the executable (or
/// of the build-time crate dir) that holds pyproject.toml and backend/osint.
fn project_root() -> Option<PathBuf> {
    if let Ok(p) = std::env::var("NOCTUA_ROOT") {
        let p = PathBuf::from(p);
        if is_root(&p) {
            return Some(p);
        }
    }
    let mut starts: Vec<PathBuf> = Vec::new();
    if let Ok(exe) = std::env::current_exe() {
        starts.push(exe);
    }
    starts.push(PathBuf::from(env!("CARGO_MANIFEST_DIR")));
    for start in starts {
        let mut cur: Option<&Path> = Some(start.as_path());
        while let Some(dir) = cur {
            if is_root(dir) {
                return Some(dir.to_path_buf());
            }
            cur = dir.parent();
        }
    }
    None
}

fn is_root(p: &Path) -> bool {
    p.join("pyproject.toml").is_file() && p.join("backend").join("osint").is_dir()
}

fn spawn_backend(root: &Path) -> std::io::Result<Child> {
    let data = root.join("data");
    let _ = std::fs::create_dir_all(&data);
    let log = std::fs::OpenOptions::new().create(true).append(true).open(data.join("desktop-backend.log"))?;
    let log2 = log.try_clone()?;
    let venv_py = root.join(".venv").join("Scripts").join("python.exe");
    let mut cmd = if venv_py.is_file() {
        let mut c = Command::new(venv_py);
        c.args(["-c", "from osint.cli import main; main(['supervise'])"]);
        c
    } else {
        let mut c = Command::new("uv");
        c.args(["run", "osint", "supervise"]);
        c
    };
    cmd.current_dir(root).stdin(Stdio::null()).stdout(Stdio::from(log)).stderr(Stdio::from(log2));
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        cmd.creation_flags(CREATE_NO_WINDOW);
    }
    cmd.spawn()
}

/// Stop the backend we started: the supervisor and its worker/API children.
fn stop_backend(state: &Shared) {
    let mut b = state.lock().unwrap();
    if let Some(mut child) = b.child.take() {
        #[cfg(windows)]
        {
            use std::os::windows::process::CommandExt;
            let _ = Command::new("taskkill")
                .args(["/PID", &child.id().to_string(), "/T", "/F"])
                .creation_flags(CREATE_NO_WINDOW)
                .status();
        }
        let _ = child.kill();
        let _ = child.wait();
    }
}

fn set_splash(win: &WebviewWindow, msg: &str, err: &str) {
    let js = format!(
        "document.getElementById('msg').textContent={};document.getElementById('err').textContent={};",
        js_str(msg),
        js_str(err)
    );
    let _ = win.eval(&js);
}

fn js_str(s: &str) -> String {
    let mut out = String::from("\"");
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '<' => out.push_str("\\u003c"),
            c => out.push(c),
        }
    }
    out.push('"');
    out
}

/// Attach to or start the backend, wait for the API, then load the globe in `win`.
fn boot(app: AppHandle, state: Shared) {
    std::thread::spawn(move || {
        let Some(win) = app.get_webview_window("main") else { return };
        if health().is_some() {
            state.lock().unwrap().attached = true;
            set_splash(&win, "Attached to running backend", "");
        } else {
            match project_root() {
                None => {
                    set_splash(&win, "Backend not found", "Set NOCTUA_ROOT to the OSINT project folder (the one with pyproject.toml).");
                    return;
                }
                Some(root) => match spawn_backend(&root) {
                    Ok(child) => {
                        let mut b = state.lock().unwrap();
                        b.child = Some(child);
                        b.attached = false;
                    }
                    Err(e) => {
                        set_splash(&win, "Could not start backend", &format!("{e}\nLog: data/desktop-backend.log"));
                        return;
                    }
                },
            }
        }
        let t0 = Instant::now();
        loop {
            if health().is_some() {
                break;
            }
            {
                let mut b = state.lock().unwrap();
                if let Some(child) = b.child.as_mut() {
                    if let Ok(Some(code)) = child.try_wait() {
                        drop(b);
                        set_splash(&win, "Backend exited", &format!("exit {code}. See data/desktop-backend.log and data/supervisor.log"));
                        return;
                    }
                }
            }
            if t0.elapsed() > BOOT_TIMEOUT {
                set_splash(&win, "Backend did not answer", "Timed out waiting for http://127.0.0.1:8765/health");
                return;
            }
            set_splash(&win, &format!("Starting backend… {}s", t0.elapsed().as_secs()), "");
            std::thread::sleep(Duration::from_millis(500));
        }
        if let Ok(url) = Url::parse(&format!("http://{HOST}:{PORT}/")) {
            let _ = win.navigate(url);
        }
    });
}

fn main() {
    let state: Shared = Arc::new(Mutex::new(Backend::default()));
    let app = tauri::Builder::default()
        .setup({
            let state = state.clone();
            move |app| {
                WebviewWindowBuilder::new(app, "main", WebviewUrl::App("index.html".into()))
                    .title("NOCTUA // Global Watch")
                    .inner_size(1600.0, 960.0)
                    .min_inner_size(1100.0, 700.0)
                    .maximized(true)
                    .background_color(tauri::window::Color(3, 6, 10, 255))
                    .build()?;

                let show = MenuItem::with_id(app, "show", "Show NOCTUA", true, None::<&str>)?;
                let reload = MenuItem::with_id(app, "reload", "Reload globe", true, None::<&str>)?;
                let restart = MenuItem::with_id(app, "restart", "Restart backend", true, None::<&str>)?;
                let quit = MenuItem::with_id(app, "quit", "Quit", true, None::<&str>)?;
                let sep = PredefinedMenuItem::separator(app)?;
                let menu = Menu::with_items(app, &[&show, &reload, &restart, &sep, &quit])?;
                let tray_state = state.clone();
                TrayIconBuilder::with_id("noctua")
                    .icon(app.default_window_icon().cloned().expect("bundle icon"))
                    .tooltip("NOCTUA // Global Watch")
                    .menu(&menu)
                    .on_menu_event(move |app, ev| match ev.id().as_ref() {
                        "show" => {
                            if let Some(w) = app.get_webview_window("main") {
                                let _ = w.unminimize();
                                let _ = w.show();
                                let _ = w.set_focus();
                            }
                        }
                        "reload" => {
                            if let Some(w) = app.get_webview_window("main") {
                                let _ = w.eval("location.reload()");
                            }
                        }
                        "restart" => {
                            let attached = tray_state.lock().unwrap().attached;
                            if attached {
                                if let Some(w) = app.get_webview_window("main") {
                                    let _ = w.eval("alert('This backend was started outside the app; restart it there.')");
                                }
                                return;
                            }
                            stop_backend(&tray_state);
                            if let Some(w) = app.get_webview_window("main") {
                                // bundled splash page (Windows serves app assets on http://tauri.localhost)
                                let splash = if cfg!(windows) { "http://tauri.localhost/index.html" } else { "tauri://localhost/index.html" };
                                if let Ok(u) = Url::parse(splash) {
                                    let _ = w.navigate(u);
                                }
                            }
                            std::thread::sleep(Duration::from_millis(800));
                            boot(app.clone(), tray_state.clone());
                        }
                        "quit" => app.exit(0),
                        _ => {}
                    })
                    .build(app)?;

                boot(app.handle().clone(), state.clone());
                Ok(())
            }
        })
        .build(tauri::generate_context!())
        .expect("failed to build NOCTUA desktop app");

    app.run(move |_app, event| {
        if let RunEvent::Exit = event {
            stop_backend(&state);
        }
    });
}
