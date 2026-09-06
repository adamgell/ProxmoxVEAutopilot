use super::{DockerOwned, OWNERSHIP_FORMAT, PGDATA, STORAGE_FORMAT, process};
use std::time::Duration;

pub struct DockerStorageAudit {
    owned: DockerOwned,
    created_ns: Option<i64>,
}
fn text(output: std::process::Output) -> Result<String, AuditError> {
    if !output.status.success() {
        return Err(AuditError::Process);
    }
    String::from_utf8(output.stdout).map_err(|_| AuditError::Process)
}
impl DockerStorageAudit {
    pub(super) fn new(owned: &DockerOwned) -> Result<Self, AuditError> {
        let id = owned.id.as_deref().ok_or(AuditError::Ownership)?;
        if id.len() != 64
            || !id
                .bytes()
                .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
            || owned.cleanup_script.is_some()
        {
            return Err(AuditError::Ownership);
        }
        Ok(Self {
            owned: DockerOwned {
                id: Some(id.into()),
                name: owned.name.clone(),
                endpoint: owned.endpoint.clone(),
                cleanup_script: None,
            },
            created_ns: None,
        })
    }
    pub fn identity(&self) -> (&str, &str) {
        (&self.owned.name, self.owned.id.as_deref().unwrap())
    }
    async fn verify(&self) -> Result<(), AuditError> {
        let out = process::docker(&[
            "--host",
            &self.owned.endpoint,
            "inspect",
            "--format",
            OWNERSHIP_FORMAT,
            self.identity().1,
        ])
        .await
        .map_err(|_| AuditError::Process)?;
        if self.owned.verified_owned_id(&out).as_deref() != Some(self.identity().1) {
            return Err(AuditError::Ownership);
        }
        Ok(())
    }
    async fn initialize(&mut self) -> Result<(), AuditError> {
        let fmt = r#"{"created":{{json .Created}},"image":{{json .Image}}}"#;
        let raw = text(
            process::docker(&[
                "--host",
                &self.owned.endpoint,
                "inspect",
                "--format",
                fmt,
                self.identity().1,
            ])
            .await
            .map_err(|_| AuditError::Process)?,
        )?;
        let value: serde_json::Value =
            serde_json::from_str(&raw).map_err(|_| AuditError::Storage)?;
        let created = chrono::DateTime::parse_from_rfc3339(
            value["created"].as_str().ok_or(AuditError::Storage)?,
        )
        .map_err(|_| AuditError::Storage)?
        .timestamp_nanos_opt()
        .filter(|v| *v > 0)
        .ok_or(AuditError::Storage)?;
        let image = value["image"].as_str().ok_or(AuditError::Storage)?;
        let digest = image.strip_prefix("sha256:").ok_or(AuditError::Storage)?;
        if digest.len() != 64
            || !digest
                .bytes()
                .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
        {
            return Err(AuditError::Storage);
        }
        let raw = text(
            process::docker(&[
                "--host",
                &self.owned.endpoint,
                "image",
                "inspect",
                "--format",
                "{{json .Config.Volumes}}",
                image,
            ])
            .await
            .map_err(|_| AuditError::Process)?,
        )?;
        let volumes: serde_json::Value =
            serde_json::from_str(&raw).map_err(|_| AuditError::Storage)?;
        if volumes != serde_json::json!({(PGDATA):{}}) {
            return Err(AuditError::Storage);
        }
        eprintln!(
            "owned_storage_image {} {} {}",
            self.identity().0,
            self.identity().1,
            image
        );
        self.created_ns = Some(created);
        Ok(())
    }
    pub async fn sample(&mut self) -> Result<StorageSample, AuditError> {
        tokio::time::timeout(Duration::from_secs(24), async {
            self.verify().await?;
            if self.created_ns.is_none() {
                self.initialize().await?;
            }
            let out = process::docker(&[
                "--host",
                &self.owned.endpoint,
                "inspect",
                "--format",
                STORAGE_FORMAT,
                self.identity().1,
            ])
            .await
            .map_err(|_| AuditError::Process)?;
            if !self.owned.storage_admitted(&out) {
                return Err(AuditError::Storage);
            }
            let raw = text(
                process::docker(&[
                    "--host",
                    &self.owned.endpoint,
                    "exec",
                    self.identity().1,
                    "stat",
                    "-f",
                    "-c",
                    "%T|%S|%b|%a",
                    PGDATA,
                ])
                .await
                .map_err(|_| AuditError::Process)?,
            )?;
            let (capacity_bytes, available_bytes) = capacity(&raw)?;
            let raw = text(
                process::docker(&[
                    "--host",
                    &self.owned.endpoint,
                    "exec",
                    self.identity().1,
                    "cat",
                    "/sys/fs/cgroup/memory.current",
                    "/sys/fs/cgroup/memory.events",
                ])
                .await
                .map_err(|_| AuditError::Process)?,
            )?;
            let memory_bytes = memory(&raw)?;
            let raw = text(
                process::docker(&[
                    "--host",
                    &self.owned.endpoint,
                    "inspect",
                    "--format",
                    "{{.State.OOMKilled}}|{{.HostConfig.Memory}}",
                    self.identity().1,
                ])
                .await
                .map_err(|_| AuditError::Process)?,
            )?;
            if raw.trim() != "false|0" {
                return Err(AuditError::Memory);
            }
            let sample = StorageSample {
                capacity_bytes,
                available_bytes,
                memory_bytes,
            };
            eprintln!(
                "owned_storage_sample {} {} {:?}",
                self.identity().0,
                self.identity().1,
                sample
            );
            Ok(sample)
        })
        .await
        .map_err(|_| AuditError::Process)?
    }
    pub async fn confirm_removed(&self) -> Result<(), AuditError> {
        tokio::time::timeout(Duration::from_secs(12),async {
            let created=self.created_ns.ok_or(AuditError::History)?;
            for filter in [format!("id={}",self.identity().1),format!("name=^/{}$",self.identity().0)] {
                let raw=text(process::docker(&["--host",&self.owned.endpoint,"ps","--all","--no-trunc","--filter",&filter,"--format","{{.ID}}"])
                    .await.map_err(|_| AuditError::Process)?)?;
                if !raw.trim().is_empty() { return Err(AuditError::Absence); }
            }
            let template=r#"[{{.TimeNano}},{{if and (eq .Type "container") (eq .Actor.ID "OWNED_ID")}}{{if eq .Action "create"}}1{{else if eq .Action "destroy"}}2{{else}}0{{end}}{{else if and (eq .Type "volume") (eq (index .Actor.Attributes "container") "OWNED_ID")}}{{if eq .Action "mount"}}3{{else if eq .Action "unmount"}}4{{else}}0{{end}}{{else}}0{{end}}]"#
                .replace("OWNED_ID",self.identity().1);
            let raw=text(process::docker(&["--host",&self.owned.endpoint,"events","--since","1970-01-01T00:00:00Z","--until","0s","--format",&template])
                .await.map_err(|_| AuditError::Process)?)?;
            events(&raw,created)?;
            eprintln!("owned_storage_removed {} {} bounded_recorded_event_bracket_only",self.identity().0,self.identity().1);
            Ok(())
        }).await.map_err(|_| AuditError::Process)?
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum AuditError {
    Ownership,
    Process,
    Storage,
    Capacity,
    Memory,
    History,
    Absence,
}
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct StorageSample {
    pub capacity_bytes: u64,
    pub available_bytes: u64,
    pub memory_bytes: u64,
}

fn capacity(text: &str) -> Result<(u64, u64), AuditError> {
    if text.len() > 8192 || !text.ends_with('\n') {
        return Err(AuditError::Capacity);
    }
    let fields: Vec<_> = text.trim().split('|').collect();
    if fields.len() != 4 || fields[0] != "tmpfs" {
        return Err(AuditError::Capacity);
    }
    let block = fields[1].parse::<u64>().map_err(|_| AuditError::Capacity)?;
    let total = fields[2].parse::<u64>().map_err(|_| AuditError::Capacity)?;
    let available = fields[3].parse::<u64>().map_err(|_| AuditError::Capacity)?;
    let total = block.checked_mul(total).ok_or(AuditError::Capacity)?;
    let available = block.checked_mul(available).ok_or(AuditError::Capacity)?;
    if total != 1073741824 || available < 134217728 || available > total {
        return Err(AuditError::Capacity);
    }
    Ok((total, available))
}
fn memory(text: &str) -> Result<u64, AuditError> {
    if text.len() > 8192 || !text.ends_with('\n') {
        return Err(AuditError::Memory);
    }
    let mut lines = text.lines();
    let current = lines
        .next()
        .ok_or(AuditError::Memory)?
        .parse::<u64>()
        .map_err(|_| AuditError::Memory)?;
    let mut values = std::collections::BTreeMap::new();
    for line in lines {
        let pair: Vec<_> = line.split_whitespace().collect();
        if pair.len() != 2 {
            return Err(AuditError::Memory);
        }
        let value = pair[1].parse::<u64>().map_err(|_| AuditError::Memory)?;
        if values.insert(pair[0], value).is_some() {
            return Err(AuditError::Memory);
        }
    }
    if values.get("oom") != Some(&0) || values.get("oom_kill") != Some(&0) {
        return Err(AuditError::Memory);
    }
    Ok(current)
}
fn events(text: &str, created: i64) -> Result<(), AuditError> {
    if text.len() > 8192 || !text.ends_with('\n') || created <= 0 {
        return Err(AuditError::History);
    }
    let rows = text
        .lines()
        .map(serde_json::from_str::<(i64, u8)>)
        .collect::<Result<Vec<_>, _>>()
        .map_err(|_| AuditError::History)?;
    if !(3..=256).contains(&rows.len())
        || rows[0].0 >= created
        || rows.iter().any(|(time, class)| *time <= 0 || *class > 4)
        || rows.windows(2).any(|pair| pair[0].0 > pair[1].0)
    {
        return Err(AuditError::History);
    }
    if rows.iter().any(|(_, class)| matches!(class, 3 | 4)) {
        return Err(AuditError::Storage);
    }
    let starts: Vec<_> = rows.iter().filter(|(_, class)| *class == 1).collect();
    let ends: Vec<_> = rows.iter().filter(|(_, class)| *class == 2).collect();
    if starts.len() != 1 || ends.len() != 1 || starts[0].0 < created || starts[0].0 > ends[0].0 {
        return Err(AuditError::History);
    }
    let start = rows
        .iter()
        .position(|(_, class)| *class == 1)
        .ok_or(AuditError::History)?;
    let end = rows
        .iter()
        .position(|(_, class)| *class == 2)
        .ok_or(AuditError::History)?;
    if start >= end {
        return Err(AuditError::History);
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn capacity_requires_actual_fixed_tmpfs_and_reserve() {
        assert_eq!(
            capacity("tmpfs|4096|262144|65536\n"),
            Ok((1073741824, 268435456))
        );
        for input in [
            "ext2/ext3|4096|262144|65536\n",
            "tmpfs|4096|524288|65536\n",
            "tmpfs|4096|262144|1\n",
            "tmpfs|18446744073709551615|2|2\n",
            "tmpfs|0|0|0\n",
            "tmpfs|4096|262144|262145\n",
            "tmpfs|4096|262144\n",
        ] {
            assert_eq!(capacity(input), Err(AuditError::Capacity));
        }
    }
    #[test]
    fn memory_requires_numeric_complete_non_oom_counters() {
        assert_eq!(
            memory("12345\nlow 0\nhigh 0\nmax 0\noom 0\noom_kill 0\n"),
            Ok(12345)
        );
        for input in [
            "123\noom 1\noom_kill 0\n",
            "123\noom 0\noom_kill 1\n",
            "123\noom 0\n",
            "123\noom 0\noom 0\noom_kill 0\n",
            "x\noom 0\noom_kill 0\n",
        ] {
            assert_eq!(memory(input), Err(AuditError::Memory));
        }
    }
    #[test]
    fn retained_events_require_a_bracket_without_attributed_mounts() {
        assert_eq!(events("[1,0]\n[3,1]\n[4,2]\n", 2), Ok(()));
        let mut full = String::from("[1,0]\n[3,1]\n");
        for _ in 0..253 {
            full.push_str("[3,0]\n");
        }
        full.push_str("[4,2]\n");
        assert_eq!(events(&full, 2), Ok(()));
        for input in [
            "[3,1]\n[4,2]\n",
            "[2,0]\n[3,1]\n[4,2]\n",
            "[1,0]\n[4,2]\n[3,1]\n",
            "[1,0]\n[3,1]\n[3,1]\n[4,2]\n",
            "[1,0]\n[3,1]\n[4,2]",
            "[1,0]\n[3,1,0]\n[4,2]\n",
            "[-1,0]\n[3,1]\n[4,2]\n",
            "[1,0]\n[3,5]\n[4,2]\n",
        ] {
            assert_eq!(events(input, 2), Err(AuditError::History));
        }
        full.push_str("[5,0]\n");
        assert_eq!(events(&full, 2), Err(AuditError::History));
        for class in [3, 4] {
            assert_eq!(
                events(&format!("[1,0]\n[3,1]\n[3,{class}]\n[4,2]\n"), 2),
                Err(AuditError::Storage)
            );
        }
    }
}
