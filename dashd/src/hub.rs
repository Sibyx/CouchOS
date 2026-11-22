//! Widgets, their store and their listeners, behind one small API used by
//! both the scheduler (polled widgets) and the HTTP server (client input).

use std::sync::Arc;

use serde_json::Value;

use crate::error::{Error, Result};
use crate::log;
use crate::notify::Notifier;
use crate::store::Store;
use crate::widget::{Ctx, Output, Refresh, Widget};

pub struct Slot {
    pub id: String,
    pub widget: Box<dyn Widget>,
}

pub struct Hub {
    pub slots: Vec<Slot>,
    pub store: Store,
    pub ctx: Ctx,
    notifier: Arc<dyn Notifier>,
}

impl Hub {
    pub fn new(slots: Vec<Slot>, ctx: Ctx, notifier: Arc<dyn Notifier>) -> Self {
        let store = Store::new(slots.iter().map(|s| (s.id.clone(), s.widget.kind())));
        let hub = Self { slots, store, ctx, notifier };
        for slot in &hub.slots {
            if let Some(output) = slot.widget.initial(&hub.ctx) {
                hub.store.put(&slot.id, output, hub.ctx.clock.now().unix);
            }
        }
        hub
    }

    pub fn slot(&self, id: &str) -> Result<&Slot> {
        self.slots.iter().find(|s| s.id == id).ok_or_else(|| Error::not_found(format!("no widget {id:?}")))
    }

    fn publish(&self, version: Option<u64>) {
        if let Some(version) = version {
            self.notifier.changed(version);
        }
    }

    fn accept(&self, id: &str, output: Output) {
        self.publish(self.store.put(id, output, self.ctx.clock.now().unix));
    }

    /// Poll one widget; failures are recorded on it, never propagated.
    pub fn refresh(&self, slot: &Slot) -> bool {
        match slot.widget.refresh(&self.ctx) {
            Ok(Refresh::Changed(output)) => self.accept(&slot.id, output),
            Ok(Refresh::Unchanged) => {}
            Err(err) => {
                log::warn(&format!("{}: {err}", slot.id));
                self.publish(self.store.fail(&slot.id, &err.message));
                return false;
            }
        }
        true
    }

    /// Client input for a pushed or stateful widget.
    pub fn handle(&self, id: &str, input: Value) -> Result<()> {
        let slot = self.slot(id)?;
        let output = slot.widget.handle(&self.ctx, input)?;
        self.accept(id, output);
        Ok(())
    }
}
