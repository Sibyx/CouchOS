//! SVG to PNG. Kodi cannot draw SVG, so the pictures dashd composes (the
//! duck, the QR code) are rasterized here once per change and served as
//! files; clients that can draw SVG get the source instead.

use resvg::tiny_skia::{Pixmap, Transform};
use resvg::usvg::{Options, Tree};

use crate::error::{Error, Result};

/// Render `svg` to a `width` x `height` PNG, scaled to fit.
pub fn png(svg: &str, width: u32, height: u32) -> Result<Vec<u8>> {
    let tree = Tree::from_str(svg, &Options::default()).map_err(|e| Error::internal(format!("svg: {e}")))?;
    let mut pixmap = Pixmap::new(width, height).ok_or_else(|| Error::internal("svg: empty size"))?;
    let size = tree.size();
    let transform = Transform::from_scale(width as f32 / size.width(), height as f32 / size.height());
    resvg::render(&tree, transform, &mut pixmap.as_mut());
    pixmap.encode_png().map_err(|e| Error::internal(format!("png: {e}")))
}

/// A short, stable name for content (FNV-1a, 64 bit): asset file names
/// change exactly when the picture does, which is what Kodi's URL-keyed
/// image cache needs.
pub fn content_hash(text: &str) -> String {
    let mut h: u64 = 0xcbf2_9ce4_8422_2325;
    for b in text.bytes() {
        h ^= u64::from(b);
        h = h.wrapping_mul(0x0100_0000_01b3);
    }
    format!("{h:016x}")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn renders_a_png() {
        let png = png(r##"<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"><rect width="10" height="10" fill="#f00"/></svg>"##, 20, 20).unwrap();
        assert_eq!(&png[1..4], b"PNG");
        assert!(super::png("<svg", 10, 10).is_err());
    }

    #[test]
    fn hash_is_stable() {
        assert_eq!(content_hash("duck"), content_hash("duck"));
        assert_ne!(content_hash("duck"), content_hash("frog"));
        assert_eq!(content_hash("").len(), 16);
    }
}
