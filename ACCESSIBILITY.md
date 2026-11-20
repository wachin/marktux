# Manual accessibility checks

PyQt6 does not expose `QAccessible` in this installation, so these checks are
**manual**. Qt still publishes its standard widgets through the AT-SPI
(Assistive Technology Service Provider Interface) bridge; Orca and Accerciser
read that bridge. Run the checks through **DeepSeek Harness** on a desktop
session, not in CI.

## Tools

```bash
sudo apt install orca accerciser
gsettings set org.gnome.desktop.interface toolkit-accessibility true
```

- **Orca** — GNOME screen reader (`orca`, `orca --replace`).
- **Accerciser** — AT-SPI tree inspector.

## Setup

1. Start MarkTux with the golden document, or open any Markdown file.
2. Start Accerciser and select the MarkTux window in its application list.
3. Start Orca and focus the editor.

## Expected hierarchy

Accerciser should show:

```
MarkTux (application)
  └─ MarkTux (window)
       └─ Markdown editor (editable text)
```

If the top node still reads `main.py`, that is the Python process node: expand
it to reach the `MarkTux` window and the `Markdown editor` text widget. The
window and application names are set in `main()`; the editor sets its name in
`MTextEdit.__init__`.

## Checklist

### Editor

- [ ] `MTextEdit` appears in the tree as an editable text widget.
- [ ] Its accessible **name** is `Markdown editor`.
- [ ] Its **description** mentions concealed syntax markers.
- [ ] The text interface reports the document character count.
- [ ] Orca reads the current line, word and character.
- [ ] Orca follows the caret with arrow keys and announces selections.
- [ ] Concealed markers do not make the spoken text skip content.

### Application chrome

- [ ] The notebook tree, search field and status bar have names/roles.
- [ ] Tab reaches the editor, tree, search and find/replace controls.
- [ ] Menu actions are announced with their shortcuts.

### Dialogs

- [ ] **Insert Image** exposes the preview, *Alternative text* and *Title*
      fields by name.
- [ ] Find/replace fields and buttons are announced.
- [ ] Errors such as “Not a file” are readable from the status bar.

## Recording results

Note the Qt/PyQt6 versions, desktop, AT-SPI version, and whether Orca or
Accerciser was used. A failing item should become an issue with the exact
widget, role and spoken text.
