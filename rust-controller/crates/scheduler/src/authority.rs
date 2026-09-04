#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ExecutorKind {
    Python,
    Rust,
}

impl ExecutorKind {
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Python => "python",
            Self::Rust => "rust",
        }
    }

    pub(crate) fn from_persisted(value: &str) -> Option<Self> {
        match value {
            "python" => Some(Self::Python),
            "rust" => Some(Self::Rust),
            _ => None,
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct AuthoritySnapshot {
    pub(crate) executor_kind: ExecutorKind,
    pub(crate) generation: i64,
}

impl AuthoritySnapshot {
    pub(crate) const fn new(executor_kind: ExecutorKind, generation: i64) -> Self {
        Self {
            executor_kind,
            generation,
        }
    }

    #[must_use]
    pub const fn executor_kind(self) -> ExecutorKind {
        self.executor_kind
    }

    #[must_use]
    pub const fn generation(self) -> i64 {
        self.generation
    }
}
