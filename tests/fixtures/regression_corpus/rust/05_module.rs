use std::collections::HashMap;

pub fn count_chars(s: &str) -> HashMap<char, u32> {
    let mut out = HashMap::new();
    for c in s.chars() {
        *out.entry(c).or_insert(0) += 1;
    }
    out
}
