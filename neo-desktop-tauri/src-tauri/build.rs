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
            // Re-sign with a STABLE identity so macOS TCC remembers the microphone
            // grant across dev rebuilds. `swiftc` leaves the binary ad-hoc/linker
            // -signed, whose identity is the cdhash -> changes on every recompile,
            // so the Live Mode mic permission never sticks. See codesign_notch_host.
            codesign_notch_host(&output);
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

/// Signs `NeoNotchHost` with a persistent code-signing identity so the TCC
/// microphone grant survives dev rebuilds (identity = cert + CFBundleIdentifier,
/// which is stable, instead of the recompile-varying cdhash of an ad-hoc build).
///
/// Identity resolution (a dedicated self-signed dev cert — NEVER the Apple
/// Development / company identity):
///   1. `NEO_NOTCH_SIGN_IDENTITY` env var (SHA-1 hash or exact cert name), if set.
///   2. A self-signed identity named "Neo Notch Dev" in the login keychain
///      (created once via scripts/setup-notch-signing.sh).
///   3. None found -> leave the ad-hoc signature untouched (old behavior).
fn codesign_notch_host(binary: &std::path::Path) {
    let identity = match resolve_signing_identity() {
        Some(id) => id,
        None => {
            println!(
                "cargo:warning=No 'Neo Notch Dev' signing identity found; NeoNotchHost stays \
                 ad-hoc signed and the mic permission will NOT persist across rebuilds. Run \
                 scripts/setup-notch-signing.sh once (or set NEO_NOTCH_SIGN_IDENTITY)."
            );
            return;
        }
    };

    // No `--options runtime`: hardened runtime would require an audio-input
    // entitlement. For a dev helper the embedded NSMicrophoneUsageDescription is
    // enough, and the Designated Requirement is still anchored to cert+identifier.
    let result = std::process::Command::new("codesign")
        .arg("--force")
        .arg("--sign")
        .arg(&identity)
        .arg("--identifier")
        .arg("com.neo.desktop.notchhost")
        .arg("--timestamp=none")
        .arg(binary)
        .output();

    match result {
        Ok(out) if out.status.success() => {
            println!(
                "cargo:warning=Signed NeoNotchHost with '{}' (mic permission will persist)",
                identity
            );
        }
        Ok(out) => {
            let stderr = String::from_utf8_lossy(&out.stderr);
            println!("cargo:warning=codesign of NeoNotchHost failed: {}", stderr);
        }
        Err(err) => {
            println!("cargo:warning=Unable to invoke codesign for NeoNotchHost: {}", err);
        }
    }
}

fn resolve_signing_identity() -> Option<String> {
    if let Ok(id) = std::env::var("NEO_NOTCH_SIGN_IDENTITY") {
        let id = id.trim().to_string();
        if !id.is_empty() {
            return Some(id);
        }
    }

    // Only ever match the dedicated self-signed dev cert. We deliberately do NOT
    // fall back to an "Apple Development" identity — that belongs to the user's
    // employer and must not be used to sign this project's artifacts.
    //
    // NOTE: no `-v` here. Our self-signed cert is untrusted (CSSMERR_TP_NOT_TRUSTED),
    // so `-v` (valid-only) would hide it — but codesign can still sign with it and
    // TCC still honors its Designated Requirement (trust is irrelevant to both).
    let out = std::process::Command::new("security")
        .args(["find-identity", "-p", "codesigning"])
        .output()
        .ok()?;
    let listing = String::from_utf8_lossy(&out.stdout);
    for line in listing.lines() {
        if line.contains("Neo Notch Dev") {
            // Line format: `  1) <40-hex-sha1> "Neo Notch Dev"`
            if let Some(hash) = line.split_whitespace().nth(1) {
                if hash.len() == 40 && hash.chars().all(|c| c.is_ascii_hexdigit()) {
                    return Some(hash.to_string());
                }
            }
        }
    }
    None
}
