//! Every fleet entry carries its skill — the "name tag" a model reads before
//! it ever loads the server's schemas.
//!
//! dangler hides tool schemas until `load_server`, which is the point, but it
//! also means nothing in a fresh session says *when* to reach for a server.
//! That sentence is a skill: a `SKILL.md` whose frontmatter `description` the
//! client indexes in every session. So the rule is forced, not optional: an
//! extension or wrapper ships `SKILL.md` next to its code, dangler installs it
//! into the client's skills directory at every start, and an entry without one
//! is not served — it is listed as `disabled` with the path it was expected at.
//!
//! Installed copies carry a marker line; dangler only ever overwrites or removes
//! a file that carries it, so a hand-written skill is never clobbered.

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

use anyhow::{Context, Result, anyhow};

use crate::config::{Config, ServerSpec};

/// The line dangler stamps into every copy it installs, right under the frontmatter.
const MARKER: &str = "<!-- installed by dangler";

/// What happened to one fleet entry's skill during a sync.
#[derive(Debug, Clone)]
pub struct SkillOutcome {
    pub server: String,
    /// The skill's frontmatter `name`, which is also the installed folder name.
    pub name: Option<String>,
    /// The `SKILL.md` the entry carries, when one was found.
    pub source: Option<PathBuf>,
    /// Where the copy landed (or already was, up to date).
    pub installed: Option<PathBuf>,
    /// Why the entry has no usable skill. `Some` here disables the server.
    pub problem: Option<String>,
}

/// The result of a sync: per-entry outcomes plus stale copies dangler removed.
#[derive(Debug, Default)]
pub struct SyncReport {
    pub outcomes: Vec<SkillOutcome>,
    pub pruned: Vec<PathBuf>,
    pub skills_dir: PathBuf,
}

impl SyncReport {
    pub fn missing(&self) -> impl Iterator<Item = &SkillOutcome> {
        self.outcomes.iter().filter(|o| o.problem.is_some())
    }
}

/// Where installed skills go: `DANGLER_SKILLS_DIR`, else the config's
/// `skills_dir`, else `~/.claude/skills`.
pub fn skills_dir(config: &Config) -> PathBuf {
    if let Some(p) = std::env::var_os("DANGLER_SKILLS_DIR") {
        return p.into();
    }
    if let Some(p) = &config.skills_dir {
        return p.clone();
    }
    home().join(".claude").join("skills")
}

fn home() -> PathBuf {
    std::env::var_os("USERPROFILE")
        .or_else(|| std::env::var_os("HOME"))
        .map(PathBuf::from)
        .unwrap_or_default()
}

/// The `extensions/` tree first-party servers live in: `DANGLER_EXTENSIONS`,
/// else the config's `extensions_dir`, else the checkout the running binary was
/// built in (`<checkout>/target/<profile>/dangler.exe` → `<checkout>/extensions`).
pub fn extensions_dir(config: &Config) -> Option<PathBuf> {
    if let Some(p) = std::env::var_os("DANGLER_EXTENSIONS") {
        return Some(p.into());
    }
    if let Some(p) = &config.extensions_dir {
        return Some(p.clone());
    }
    let exe = std::env::current_exe().ok()?;
    exe.ancestors()
        .map(|a| a.join("extensions"))
        .find(|p| p.is_dir())
}

/// The `SKILL.md` a fleet entry carries, in order of precedence:
/// an explicit `skill` (file or directory), the `--directory` an `uv run`-style
/// command points at, else `<extensions_dir>/<server>/SKILL.md`.
pub fn resolve_source(name: &str, spec: &ServerSpec, extensions: Option<&Path>) -> Result<PathBuf> {
    if let Some(p) = &spec.skill {
        let file = if p.is_dir() {
            p.join("SKILL.md")
        } else {
            p.clone()
        };
        return if file.is_file() {
            Ok(file)
        } else {
            Err(anyhow!(
                "`skill` names {}, which does not exist",
                file.display()
            ))
        };
    }
    let mut candidates = Vec::new();
    if let Some(i) = spec.args.iter().position(|a| a == "--directory")
        && let Some(dir) = spec.args.get(i + 1)
    {
        candidates.push(PathBuf::from(dir).join("SKILL.md"));
    }
    match extensions {
        Some(ext) => candidates.push(ext.join(name).join("SKILL.md")),
        None if candidates.is_empty() => {
            return Err(anyhow!(
                "no `skill` set and no extensions directory known (set `extensions_dir` in the config or DANGLER_EXTENSIONS)"
            ));
        }
        None => {}
    }
    candidates
        .iter()
        .find(|p| p.is_file())
        .cloned()
        .ok_or_else(|| {
            let looked = candidates
                .iter()
                .map(|p| p.display().to_string())
                .collect::<Vec<_>>()
                .join(" or ");
            anyhow!("no SKILL.md at {looked} — every fleet entry carries its skill")
        })
}

/// A parsed `SKILL.md`: the frontmatter block, its `name`, and the body after it.
struct Skill {
    name: String,
    frontmatter: String,
    body: String,
}

/// Split the frontmatter off and require a `name` and a `description`, since
/// the client indexes skills by exactly those two fields.
fn parse(raw: &str) -> Result<Skill> {
    let text = raw.strip_prefix('\u{feff}').unwrap_or(raw);
    let mut lines = text.lines();
    if lines.next().map(str::trim) != Some("---") {
        anyhow::bail!("SKILL.md must start with a `---` frontmatter block");
    }
    let mut frontmatter = Vec::new();
    let mut closed = false;
    for line in lines.by_ref() {
        if line.trim() == "---" {
            closed = true;
            break;
        }
        frontmatter.push(line);
    }
    if !closed {
        anyhow::bail!("frontmatter is never closed with `---`");
    }
    let field = |key: &str| {
        frontmatter
            .iter()
            .find_map(|l| l.strip_prefix(key).and_then(|r| r.strip_prefix(':')))
            .map(|v| v.trim().trim_matches('"').trim_matches('\'').to_string())
            .filter(|v| !v.is_empty())
    };
    let name = field("name").ok_or_else(|| anyhow!("frontmatter has no `name:`"))?;
    if !name
        .bytes()
        .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'-')
        || name.starts_with('-')
    {
        anyhow::bail!("skill name '{name}' must be lowercase letters, digits and dashes");
    }
    field("description").ok_or_else(|| anyhow!("frontmatter has no `description:`"))?;
    let body: String = lines.collect::<Vec<_>>().join("\n");
    Ok(Skill {
        name,
        frontmatter: frontmatter.join("\n"),
        body,
    })
}

/// The installed text: frontmatter verbatim, then the marker, then the body.
/// Existing marker lines in the body are dropped so a re-installed copy of a
/// copy does not stack them.
fn rendered(skill: &Skill, source: &Path) -> String {
    let body: Vec<&str> = skill
        .body
        .lines()
        .filter(|l| !l.starts_with(MARKER))
        .collect();
    let body = body.join("\n");
    let body = body.trim_start_matches('\n');
    format!(
        "---\n{}\n---\n{MARKER} from {} — edit the source; this copy is rewritten at every dangler start -->\n\n{}\n",
        skill.frontmatter,
        source.display().to_string().replace('\\', "/"),
        body.trim_end()
    )
}

/// Install every fleet entry's skill and remove copies dangler installed for
/// entries that are no longer configured. Entries without a usable skill are
/// moved out of `config.servers` into `config.disabled`, so the fleet never
/// spawns them; the reason travels with them into `list_servers`.
pub fn sync(config: &mut Config) -> Result<SyncReport> {
    let dir = skills_dir(config);
    let extensions = extensions_dir(config);
    let mut report = SyncReport {
        skills_dir: dir.clone(),
        ..Default::default()
    };
    let mut keep: BTreeMap<String, PathBuf> = BTreeMap::new();
    let mut disabled = BTreeMap::new();

    for (server, spec) in &config.servers {
        let mut outcome = SkillOutcome {
            server: server.clone(),
            name: None,
            source: None,
            installed: None,
            problem: None,
        };
        match install_one(server, spec, extensions.as_deref(), &dir) {
            Ok((name, source, installed)) => {
                outcome.name = Some(name.clone());
                outcome.source = Some(source);
                outcome.installed = Some(installed.clone());
                keep.insert(name, installed);
            }
            Err(e) => outcome.problem = Some(format!("{e:#}")),
        }
        report.outcomes.push(outcome);
    }
    for o in report.missing() {
        disabled.insert(
            o.server.clone(),
            format!(
                "disabled: {}. Add a SKILL.md (frontmatter `name` + `description`) and restart dangler",
                o.problem.as_deref().unwrap_or("no skill")
            ),
        );
    }
    for (name, reason) in &disabled {
        config.servers.remove(name);
        config.disabled.insert(name.clone(), reason.clone());
    }
    config.skills = report
        .outcomes
        .iter()
        .filter_map(|o| Some((o.server.clone(), o.name.clone()?)))
        .collect();

    // Prune: a copy that carries the marker but was not produced by this sync
    // belongs to an entry that left the fleet. Only marked files are touched.
    if let Ok(entries) = std::fs::read_dir(&dir) {
        for entry in entries.flatten() {
            let path = entry.path().join("SKILL.md");
            if keep.values().any(|k| *k == path) {
                continue;
            }
            let Ok(text) = std::fs::read_to_string(&path) else {
                continue;
            };
            if text.lines().any(|l| l.starts_with(MARKER)) {
                match std::fs::remove_file(&path) {
                    Ok(()) => {
                        // remove the folder too if the skill was the only thing in it
                        let _ = std::fs::remove_dir(entry.path());
                        report.pruned.push(path);
                    }
                    Err(e) => {
                        tracing::warn!(path = %path.display(), error = %e, "could not prune stale skill")
                    }
                }
            }
        }
    }
    Ok(report)
}

/// Resolve, parse, and write one entry's skill. Returns (name, source, installed).
fn install_one(
    server: &str,
    spec: &ServerSpec,
    extensions: Option<&Path>,
    dir: &Path,
) -> Result<(String, PathBuf, PathBuf)> {
    let source = resolve_source(server, spec, extensions)?;
    let raw = std::fs::read_to_string(&source)
        .with_context(|| format!("reading {}", source.display()))?;
    let skill = parse(&raw).with_context(|| format!("{}", source.display()))?;
    let target_dir = dir.join(&skill.name);
    let target = target_dir.join("SKILL.md");
    let text = rendered(&skill, &source);
    if let Ok(existing) = std::fs::read_to_string(&target) {
        if existing == text {
            return Ok((skill.name, source, target));
        }
        if !existing.lines().any(|l| l.starts_with(MARKER)) {
            anyhow::bail!(
                "{} already exists and was not installed by dangler — move it into the extension as its SKILL.md, or delete it",
                target.display()
            );
        }
    }
    std::fs::create_dir_all(&target_dir)
        .with_context(|| format!("creating {}", target_dir.display()))?;
    std::fs::write(&target, text).with_context(|| format!("writing {}", target.display()))?;
    tracing::info!(server, skill = %skill.name, path = %target.display(), "installed skill");
    Ok((skill.name, source, target))
}

/// Human-readable report for `dangler skills` and the startup log.
pub fn describe(report: &SyncReport) -> String {
    let mut out = format!("skills → {}\n", report.skills_dir.display());
    for o in &report.outcomes {
        match (&o.name, &o.problem) {
            (Some(name), _) => out.push_str(&format!(
                "  {}: {} ← {}\n",
                o.server,
                name,
                o.source
                    .as_deref()
                    .map(|p| p.display().to_string())
                    .unwrap_or_default()
            )),
            (None, Some(p)) => out.push_str(&format!("  {}: DISABLED — {p}\n", o.server)),
            (None, None) => {}
        }
    }
    for p in &report.pruned {
        out.push_str(&format!("  pruned {}\n", p.display()));
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    fn spec() -> ServerSpec {
        toml::from_str::<Config>("[servers.x]\ncommand = \"x\"")
            .unwrap()
            .servers
            .remove("x")
            .unwrap()
    }

    fn scratch(tag: &str) -> PathBuf {
        let d = std::env::temp_dir().join(format!("dangler-skills-{tag}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&d);
        std::fs::create_dir_all(&d).unwrap();
        d
    }

    const GOOD: &str = "---\nname: alpha-tag\ndescription: Use alpha when the user wants alpha.\n---\n\n# alpha\n\nbody\n";

    #[test]
    fn frontmatter_is_required_and_named() {
        let s = parse(GOOD).unwrap();
        assert_eq!(s.name, "alpha-tag");
        assert!(s.body.contains("# alpha"));
        assert!(parse("# no frontmatter\n").is_err());
        assert!(parse("---\ndescription: x\n---\n").is_err());
        assert!(parse("---\nname: alpha\n---\n").is_err());
        assert!(parse("---\nname: Alpha Tag\ndescription: x\n---\n").is_err());
        assert!(parse("---\nname: alpha\ndescription: x\n").is_err());
    }

    #[test]
    fn rendered_copy_carries_one_marker() {
        let s = parse(GOOD).unwrap();
        let once = rendered(&s, Path::new("C:/ext/alpha/SKILL.md"));
        assert!(once.starts_with("---\nname: alpha-tag\n"));
        assert_eq!(once.matches(MARKER).count(), 1);
        assert!(once.contains("from C:/ext/alpha/SKILL.md"));
        // a copy of a copy does not stack markers
        let again = rendered(&parse(&once).unwrap(), Path::new("C:/ext/alpha/SKILL.md"));
        assert_eq!(again, once);
    }

    #[test]
    fn resolve_prefers_explicit_then_directory_then_extensions() {
        let d = scratch("resolve");
        let ext = d.join("extensions");
        std::fs::create_dir_all(ext.join("beta")).unwrap();
        std::fs::write(ext.join("beta/SKILL.md"), GOOD).unwrap();
        let mut s = spec();
        assert_eq!(
            resolve_source("beta", &s, Some(&ext)).unwrap(),
            ext.join("beta/SKILL.md")
        );
        let err = resolve_source("gamma", &s, Some(&ext))
            .unwrap_err()
            .to_string();
        assert!(err.contains("gamma"), "{err}");

        let uv = d.join("uvproj");
        std::fs::create_dir_all(&uv).unwrap();
        std::fs::write(uv.join("SKILL.md"), GOOD).unwrap();
        s.args = vec![
            "run".into(),
            "--directory".into(),
            uv.to_string_lossy().into(),
            "srv".into(),
        ];
        assert_eq!(
            resolve_source("gamma", &s, Some(&ext)).unwrap(),
            uv.join("SKILL.md")
        );

        s.skill = Some(d.join("nowhere"));
        assert!(resolve_source("gamma", &s, Some(&ext)).is_err());
        s.skill = Some(uv.clone());
        assert_eq!(
            resolve_source("gamma", &s, None).unwrap(),
            uv.join("SKILL.md")
        );
    }

    #[test]
    fn sync_installs_disables_and_prunes_only_marked_copies() {
        let d = scratch("sync");
        let ext = d.join("extensions");
        let skills = d.join("skills");
        std::fs::create_dir_all(ext.join("alpha")).unwrap();
        std::fs::write(ext.join("alpha/SKILL.md"), GOOD).unwrap();
        // a hand-written skill that must survive, and a stale dangler copy that must go
        std::fs::create_dir_all(skills.join("mine")).unwrap();
        std::fs::write(
            skills.join("mine/SKILL.md"),
            "---\nname: mine\ndescription: hand made\n---\n",
        )
        .unwrap();
        std::fs::create_dir_all(skills.join("gone")).unwrap();
        std::fs::write(
            skills.join("gone/SKILL.md"),
            format!("---\nname: gone\ndescription: old\n---\n{MARKER} from x -->\n"),
        )
        .unwrap();

        let mut cfg: Config = toml::from_str(&format!(
            "extensions_dir = \"{ext}\"\nskills_dir = \"{skills}\"\n[servers.alpha]\ncommand = \"a\"\n[servers.beta]\ncommand = \"b\"\n",
            ext = ext.display().to_string().replace('\\', "/"),
            skills = skills.display().to_string().replace('\\', "/"),
        ))
        .unwrap();
        let report = sync(&mut cfg).unwrap();

        assert!(skills.join("alpha-tag/SKILL.md").is_file());
        assert!(skills.join("mine/SKILL.md").is_file());
        assert!(!skills.join("gone/SKILL.md").exists());
        assert_eq!(report.pruned.len(), 1);
        assert_eq!(report.missing().count(), 1);
        assert!(cfg.servers.contains_key("alpha"));
        assert!(!cfg.servers.contains_key("beta"));
        assert!(cfg.disabled["beta"].contains("SKILL.md"));
        assert_eq!(cfg.skills["alpha"], "alpha-tag");

        // second run is a no-op that does not rewrite an identical copy
        let before = std::fs::metadata(skills.join("alpha-tag/SKILL.md"))
            .unwrap()
            .modified()
            .unwrap();
        sync(&mut cfg).unwrap();
        let after = std::fs::metadata(skills.join("alpha-tag/SKILL.md"))
            .unwrap()
            .modified()
            .unwrap();
        assert_eq!(before, after);

        // a hand-written file squatting the target name is a conflict, never overwritten
        std::fs::write(
            ext.join("alpha/SKILL.md"),
            GOOD.replace("alpha-tag", "mine"),
        )
        .unwrap();
        let report = sync(&mut cfg).unwrap();
        let alpha = report
            .outcomes
            .iter()
            .find(|o| o.server == "alpha")
            .unwrap();
        assert!(
            alpha
                .problem
                .as_deref()
                .unwrap()
                .contains("not installed by dangler")
        );
        assert_eq!(
            std::fs::read_to_string(skills.join("mine/SKILL.md"))
                .unwrap()
                .lines()
                .count(),
            4
        );
    }
}
