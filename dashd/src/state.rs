//! Small JSON files a widget keeps across restarts (today's frog, later the
//! TODO list), one per widget id, under $XDG_STATE_HOME/dashd.

use std::fs;
use std::path::PathBuf;

use serde::Serialize;
use serde::de::DeserializeOwned;

use crate::assets::AssetStore;
use crate::error::{Error, Result};

pub struct StateDir {
    dir: PathBuf,
}

impl StateDir {
    pub fn open(dir: PathBuf) -> Result<Self> {
        fs::create_dir_all(&dir)?;
        Ok(Self { dir })
    }

    fn path(&self, key: &str) -> Result<PathBuf> {
        if AssetStore::valid_name(key) {
            Ok(self.dir.join(format!("{key}.json")))
        } else {
            Err(Error::invalid(format!("bad state key {key:?}")))
        }
    }

    /// `None` when missing or unreadable (a corrupt file is as good as none).
    pub fn load<T: DeserializeOwned>(&self, key: &str) -> Option<T> {
        let bytes = fs::read(self.path(key).ok()?).ok()?;
        serde_json::from_slice(&bytes).ok()
    }

    pub fn save<T: Serialize>(&self, key: &str, value: &T) -> Result<()> {
        let path = self.path(key)?;
        let tmp = path.with_extension("json.tmp");
        fs::write(&tmp, serde_json::to_vec(value)?)?;
        fs::rename(tmp, path)?;
        Ok(())
    }
}
