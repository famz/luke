use std::thread;
use std::time::Duration;
use std::process::{self, Command};
use anyhow::Result;
use nc;

fn main() {
    if process::id() == 1 {
        init_main();
    }
    println!("Hello, luke!");
}

fn run_etc_init() -> Result<()> {
    let st = Command::new("/bin/sh")
        .arg("-i")
        .status()?;
    Ok(())
}

fn init_main() {
    println!("running luke as init...");
    run_etc_init().expect("run /etc/init");
    poweroff();
}

fn poweroff() {
    let opcode = 0x4321fedc;
    unsafe {
        nc::reboot(
            nc::LINUX_REBOOT_MAGIC1,
            nc::LINUX_REBOOT_MAGIC2,
            nc::LINUX_REBOOT_CMD_POWER_OFF,
            0).unwrap();
    };
}
