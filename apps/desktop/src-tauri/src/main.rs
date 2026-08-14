#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

#[cfg(not(test))]
fn main() {
    workama_desktop_lib::run();
}

#[cfg(test)]
fn main() {}
