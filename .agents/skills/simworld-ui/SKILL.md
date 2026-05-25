---
name: simworld-ui
description: Use when implementing SimWorld Studio frontend screens, StudioShell, mode-specific layouts, dashboard panels, charts, or any UI component in simworld_studio_workspace/web/src/.
---

# SimWorld UI Skill

## Before starting any UI task:

1. Read `AGENTS.md` (repo root)
2. Read `docs/product/ui_modes.md`
3. Read `docs/product/architecture.md`
4. Look at `docs/design/*.png` for visual references

## Key files:

- `simworld_studio_workspace/web/src/App.jsx` — all components (~9 000 lines)
- `simworld_studio_workspace/web/src/index.css` — all styles + CSS variables

## Shared primitives (already exist in App.jsx — use them):

```jsx
<Badge variant="blue|green|orange|red|muted|violet">text</Badge>
<SourceBadge source="builtin|custom|learned" />
<StatusBadge enabled={bool} readOnly={bool} />
<Btn variant="primary|success|danger|enable|disable|cancel|ghost" size="xs|sm|md">text</Btn>
<ToggleBtn enabled={bool} busy={bool} onClick={fn} />
<TagChip tag="buildings" />
<ModalOverlay onClose={fn} maxWidth={750}><ModalHeader .../><ModalFooter .../></ModalOverlay>
<PageHeader icon={ICONS.x(22)} title="..." subtitle="..." action={<Btn/>} />
<Eyebrow>SECTION LABEL</Eyebrow>
<Field label="Name"><input style={inputSx} /></Field>
```

## Icons (use ICONS.* only — no emoji):

```jsx
ICONS.chat(size)      ICONS.target(size)    ICONS.activity(size)
ICONS.refresh(size)   ICONS.robot(size)     ICONS.book(size)
ICONS.cube(size)      ICONS.map(size)       ICONS.check(size)
ICONS.close(size)     ICONS.warning(size)   ICONS.clock(size)
ICONS.building(size)  ICONS.tree(size)      ICONS.car(size)
ICONS.chartBar(size)  ICONS.scan(size)      ICONS.zap(size)
ICONS.collision(size) ICONS.users(size)     ICONS.folder(size)
```

## CSS variables (use these — never hardcode hex):

```css
var(--bg)          var(--panel)       var(--panel-2)     var(--panel-3)
var(--ink)         var(--ink-2)       var(--ink-3)
var(--line)        var(--line-2)
var(--blue)        var(--blue-soft)   var(--blue-2)
var(--green)       var(--green-soft)
var(--orange)      var(--orange-soft)
var(--red)         var(--violet)
var(--bg-tertiary) var(--bg-hover)
```

## CSS utility classes (in index.css):

```css
.pipeline-tab        .pipeline-tab.active
.sec-nav-btn         .sec-nav-btn.active
.artifact-chip       .artifact-chip.active   .artifact-chip.empty
.mode-card           .mode-card.current
.config-section      .config-section-title
.config-row          .config-input          .config-select
.status-row          .status-pass           .status-fail    .status-score
.filter-pill         .filter-pill.active
.act-btn             .act-btn-primary       .act-btn-danger .act-btn-cancel
.lbl-blue            .lbl-green             .lbl-muted
.primary-cta.blue    .primary-cta.green     .primary-cta.orange
.form-input          .section-eyebrow
```

## Rules:

1. **Use shared primitives** — do not write new inline-styled badge/button components
2. **No hardcoded hex colors** — use CSS variables
3. **No emoji** — use `ICONS.*` functions
4. **One mode = one job** — scene generation must not show SPL or reward charts
5. **Mock data first** — use typed fixtures before real API calls
6. **No new files** unless absolutely necessary — add components inline in App.jsx following existing patterns
7. **Cursor on inputs** — always `cursor: "text"` on textarea/input elements

## Mode panel mapping (do not deviate):

| Mode | Left Panel | Right Panel |
|------|-----------|-------------|
| scene | `ChatPanel` (titled "Intent + SimCoder") | `SceneInspectorPanel` |
| task | `TaskGenPanel` (titled "Task Builder") | `TaskInspectorPanel` |
| training | `TrainingConfigPanel` (titled "Training Config") | `AgentPanel` (titled "Agent Monitor") |
| coevolve | `CurriculumBuilderPanel` (titled "Curriculum Builder") | `RoundInspectorPanel` (titled "Round Inspector") |

## Before finishing:

```bash
cd simworld_studio_workspace/web
npx vite build --mode development   # must pass with no errors
```
