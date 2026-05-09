fn main() {
    tauri_build::build();

    let proto_path = std::env::var("NEO_PROTO_PATH")
        .unwrap_or_else(|_| "../../proto/neo_desktop.proto".to_string());

    tonic_build::configure()
        .build_server(true)
        .build_client(true)
        .compile_protos(&[&proto_path], &["../../proto"])
        .expect("Failed to compile proto");
}