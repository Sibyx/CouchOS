//! Files widgets hand to clients (the frog photo): kept on disk, not in RAM,
//! and served by name from /v1/assets/<name>.

use std::fs;
use std::path::PathBuf;

use crate::error::{Error, Result};

pub struct AssetStore {
    dir: PathBuf,
}

impl AssetStore {
    pub fn open(dir: PathBuf) -> Result<Self> {
        fs::create_dir_all(&dir)?;
        Ok(Self { dir })
    }

    /// Asset names are flat and boring on purpose: no paths, no traversal.
    pub fn valid_name(name: &str) -> bool {
        !name.is_empty()
            && name.len() <= 96
            && !name.starts_with('.')
            && name.bytes().all(|b| b.is_ascii_alphanumeric() || b"-_.".contains(&b))
    }

    fn checked(&self, name: &str) -> Result<PathBuf> {
        if Self::valid_name(name) {
            Ok(self.dir.join(name))
        } else {
            Err(Error::invalid(format!("bad asset name {name:?}")))
        }
    }

    /// Written to a temp file and renamed: a client never reads half a photo.
    pub fn put(&self, name: &str, data: &[u8]) -> Result<()> {
        let path = self.checked(name)?;
        let tmp = self.dir.join(format!(".{name}.tmp"));
        fs::write(&tmp, data)?;
        fs::rename(&tmp, &path)?;
        Ok(())
    }

    pub fn exists(&self, name: &str) -> bool {
        self.checked(name).is_ok_and(|p| p.is_file())
    }

    pub fn path(&self, name: &str) -> Result<PathBuf> {
        let path = self.checked(name)?;
        if path.is_file() { Ok(path) } else { Err(Error::not_found(format!("no asset {name}"))) }
    }

    /// Remove every asset starting with `prefix` except `keep`.
    pub fn prune(&self, prefix: &str, keep: &str) -> Result<()> {
        for entry in fs::read_dir(&self.dir)? {
            let entry = entry?;
            let name = entry.file_name();
            let Some(name) = name.to_str() else { continue };
            if name.starts_with(prefix) && name != keep {
                fs::remove_file(entry.path())?;
            }
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rejects_paths_and_hidden_names() {
        assert!(AssetStore::valid_name("frog-2026-11-22.jpg"));
        assert!(!AssetStore::valid_name("../etc/passwd"));
        assert!(!AssetStore::valid_name("a/b.jpg"));
        assert!(!AssetStore::valid_name(".hidden"));
        assert!(!AssetStore::valid_name(""));
    }

    #[test]
    fn put_prune_and_serve() {
        let dir = std::env::temp_dir().join(format!("dashd-assets-{}", std::process::id()));
        let store = AssetStore::open(dir.clone()).unwrap();
        store.put("frog-1.jpg", b"one").unwrap();
        store.put("frog-2.jpg", b"two").unwrap();
        store.prune("frog-", "frog-2.jpg").unwrap();
        assert!(!store.exists("frog-1.jpg"));
        assert_eq!(fs::read(store.path("frog-2.jpg").unwrap()).unwrap(), b"two");
        fs::remove_dir_all(dir).unwrap();
    }
}
