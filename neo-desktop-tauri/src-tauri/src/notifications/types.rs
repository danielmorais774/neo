use serde::{Deserialize, Deserializer, Serialize};

fn default_duration_ms() -> u64 {
    12000
}
fn default_position() -> String {
    "bottom_right".to_string()
}
fn default_play_sound() -> bool {
    true
}

pub fn deserialize_body_or_desc<'de, D>(deserializer: D) -> Result<Option<String>, D::Error>
where
    D: Deserializer<'de>,
{
    #[derive(Deserialize)]
    #[serde(untagged)]
    enum BodyOrDesc {
        New { description: Option<String> },
        Legacy { body: Option<String> },
        Plain(Option<String>),
    }

    let intermediate = BodyOrDesc::deserialize(deserializer)?;
    match intermediate {
        BodyOrDesc::New { description } => Ok(description),
        BodyOrDesc::Legacy { body } => Ok(body),
        BodyOrDesc::Plain(s) => Ok(s),
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct NotificationAction {
    pub action_type: String,
    pub label: String,
    #[serde(default)]
    pub payload: Option<serde_json::Value>,
    #[serde(default)]
    pub button_style: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct NotificationField {
    pub label: String,
    pub value: String,
    pub source: String,
    #[serde(default)]
    pub placeholder: Option<String>,
    #[serde(default)]
    pub field_type: Option<String>,
    #[serde(default)]
    pub options: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct NotificationConfig {
    #[serde(default = "default_duration_ms")]
    pub duration_ms: u64,
    #[serde(default = "default_position")]
    pub position: String,
    #[serde(default)]
    pub persistent: bool,
    #[serde(default = "default_play_sound")]
    pub play_sound: bool,
}

#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct NeoNotification {
    pub id: Option<String>,
    pub user_id: Option<String>,
    pub mission_id: Option<String>,
    pub kind: Option<String>,
    pub title: Option<String>,
    #[serde(default, deserialize_with = "deserialize_body_or_desc")]
    pub description: Option<String>,
    #[serde(default)]
    pub fields: Vec<NotificationField>,
    #[serde(default)]
    pub actions: Vec<NotificationAction>,
    pub payload: Option<serde_json::Value>,
    pub created_at_unix: Option<i64>,
    #[serde(default)]
    pub config: Option<NotificationConfig>,
}
