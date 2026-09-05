use pve_port::{PveAccessMode, PveBaseUrl, PveObserverConfig, ReqwestPveObserver};
use std::{
    sync::{Arc, Mutex},
    time::Duration,
};
use tokio::{
    io::{AsyncReadExt, AsyncWriteExt},
    net::TcpListener,
    task::JoinHandle,
};

pub struct Server {
    pub base: PveBaseUrl,
    pub url: String,
    pub requests: Arc<Mutex<Vec<String>>>,
    task: Option<JoinHandle<()>>,
}

impl Server {
    pub async fn start(status: u16, body: String, extra: String, delay: Duration) -> Self {
        Self::script(vec![(status, body, extra, delay)]).await
    }

    pub async fn script(responses: Vec<(u16, String, String, Duration)>) -> Self {
        assert!(!responses.is_empty());
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let url = format!("http://{}", listener.local_addr().unwrap());
        let base = PveBaseUrl::parse(&url).unwrap();
        let requests = Arc::new(Mutex::new(Vec::new()));
        let captured = Arc::clone(&requests);
        let task = tokio::spawn(async move {
            let mut next = 0;
            loop {
                let Ok(Ok((mut stream, _))) =
                    tokio::time::timeout(Duration::from_secs(3), listener.accept()).await
                else {
                    break;
                };
                let mut request = Vec::new();
                loop {
                    let mut chunk = [0; 1024];
                    let Ok(Ok(count)) =
                        tokio::time::timeout(Duration::from_secs(1), stream.read(&mut chunk)).await
                    else {
                        return;
                    };
                    if count == 0 {
                        return;
                    }
                    request.extend_from_slice(&chunk[..count]);
                    if request.windows(4).any(|part| part == b"\r\n\r\n") {
                        break;
                    }
                    if request.len() > 16_384 {
                        return;
                    }
                }
                captured
                    .lock()
                    .unwrap()
                    .push(String::from_utf8(request).unwrap());
                let (status, body, extra, delay) = &responses[next];
                next = (next + 1).min(responses.len() - 1);
                tokio::time::sleep(*delay).await;
                let response = format!(
                    "HTTP/1.1 {status} Fixture\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n{extra}\r\n{body}",
                    body.len()
                );
                let _ = stream.write_all(response.as_bytes()).await;
            }
        });
        Self {
            base,
            url,
            requests,
            task: Some(task),
        }
    }

    pub async fn json(data: serde_json::Value) -> Self {
        Self::start(
            200,
            serde_json::json!({"data": data}).to_string(),
            String::new(),
            Duration::ZERO,
        )
        .await
    }

    pub fn observer(&self) -> ReqwestPveObserver {
        ReqwestPveObserver::new(
            PveObserverConfig::new(self.base.clone(), PveAccessMode::Observe, false)
                .with_timeout(Duration::from_millis(100)),
        )
        .unwrap()
    }

    pub async fn finish(mut self) {
        let task = self.task.take().unwrap();
        task.abort();
        let result = task.await;
        assert!(
            result.is_ok() || result.unwrap_err().is_cancelled(),
            "fixture task panicked"
        );
    }
}

impl Drop for Server {
    fn drop(&mut self) {
        if let Some(task) = &self.task {
            task.abort();
        }
    }
}
