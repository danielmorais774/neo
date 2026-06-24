fn main() {
    tauri_build::build();

    build_macos_notch_host();
    build_macos_notification_host();

    let proto_path = std::env::var("NEO_PROTO_PATH")
        .unwrap_or_else(|_| "../../proto/neo_desktop.proto".to_string());

    tonic_build::configure()
        .build_server(true)
        .build_client(true)
        .compile_protos(&[&proto_path], &["../../proto"])
        .expect("Failed to compile proto");
}

fn build_macos_notification_host() {
    if std::env::var("CARGO_CFG_TARGET_OS").as_deref() != Ok("macos") {
        return;
    }

    let manifest_dir =
        std::path::PathBuf::from(std::env::var("CARGO_MANIFEST_DIR").unwrap_or_default());
    let profile = std::env::var("PROFILE").unwrap_or_else(|_| "debug".to_string());
    let source = manifest_dir.join("macos").join("NeoNotificationHost.swift");
    println!("cargo:rerun-if-changed={}", source.display());
    let output = manifest_dir
        .join("target")
        .join(&profile)
        .join("NeoNotificationHost");
    let clang_cache = manifest_dir.join("target").join("clang-module-cache");
    let _ = std::fs::create_dir_all(&clang_cache);

    if !source.exists() {
        println!(
            "cargo:warning=NeoNotificationHost.swift not found at {}",
            source.display()
        );
        return;
    }

    let output_result = std::process::Command::new("xcrun")
        .arg("swiftc")
        .arg(&source)
        .arg("-O")
        .arg("-o")
        .arg(&output)
        .env("CLANG_MODULE_CACHE_PATH", &clang_cache)
        .output();

    match output_result {
        Ok(output_result) if output_result.status.success() => {
            println!(
                "cargo:warning=Built native notification host at {}",
                output.display()
            );
        }
        Ok(output_result) => {
            let stderr = String::from_utf8_lossy(&output_result.stderr);
            println!("cargo:warning=Failed to build native notification host: {}", stderr);
        }
        Err(err) => {
            println!(
                "cargo:warning=Unable to invoke swiftc for native notification host: {}",
                err
            );
        }
    }
}

fn build_macos_notch_host() {
    if std::env::var("CARGO_CFG_TARGET_OS").as_deref() != Ok("macos") {
        return;
    }

    let manifest_dir =
        std::path::PathBuf::from(std::env::var("CARGO_MANIFEST_DIR").unwrap_or_default());
    let profile = std::env::var("PROFILE").unwrap_or_else(|_| "debug".to_string());
    let source = manifest_dir.join("macos").join("NeoNotchHost.swift");
    let info_plist = manifest_dir.join("macos").join("NeoNotchHost-Info.plist");
    println!("cargo:rerun-if-changed={}", source.display());
    println!("cargo:rerun-if-changed={}", info_plist.display());
    let output = manifest_dir
        .join("target")
        .join(&profile)
        .join("NeoNotchHost");
    let clang_cache = manifest_dir.join("target").join("clang-module-cache");
    let _ = std::fs::create_dir_all(&clang_cache);

    if !source.exists() {
        println!(
            "cargo:warning=NeoNotchHost.swift not found at {}",
            source.display()
        );
        return;
    }

    let mut command = std::process::Command::new("xcrun");
    command
        .arg("swiftc")
        .arg(&source)
        .arg("-O")
        .arg("-o")
        .arg(&output);

    // Embed Info.plist into the binary's __TEXT,__info_plist section so TCC can
    // read NSMicrophoneUsageDescription — required for native mic capture (Live
    // Mode) to work from this standalone (unbundled) helper, even when the main
    // app window is minimized.
    if info_plist.exists() {
        command
            .arg("-Xlinker")
            .arg("-sectcreate")
            .arg("-Xlinker")
            .arg("__TEXT")
            .arg("-Xlinker")
            .arg("__info_plist")
            .arg("-Xlinker")
            .arg(&info_plist);
    }

    let output_result = command.env("CLANG_MODULE_CACHE_PATH", &clang_cache).output();

    match output_result {
        Ok(output_result) if output_result.status.success() => {
            println!(
                "cargo:warning=Built native notch host at {}",
                output.display()
            );
        }
        Ok(output_result) => {
            let stderr = String::from_utf8_lossy(&output_result.stderr);
            println!("cargo:warning=Failed to build native notch host: {}", stderr);
        }
        Err(err) => {
            println!(
                "cargo:warning=Unable to invoke swiftc for native notch host: {}",
                err
            );
        }
    }
}
