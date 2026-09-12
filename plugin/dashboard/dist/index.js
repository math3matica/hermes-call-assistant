/* Hermes Call Assistant dashboard page. Plain IIFE; React is supplied by Hermes. */
(function () {
  "use strict";
  const SDK = window.__HERMES_PLUGIN_SDK__;
  if (!SDK || !window.__HERMES_PLUGINS__) return;
  const React = SDK.React;
  const h = React.createElement;
  const { useEffect, useState } = SDK.hooks;
  const { Card, CardHeader, CardTitle, CardContent, Button, Input, Label, Badge } = SDK.components;
  const api = (path, options) => SDK.fetchJSON("/api/plugins/hermes-call-assistant" + path, options);

  function RootList(props) {
    const items = props.items || [];
    return h("div", { className: "space-y-2" }, items.length ? items.map((item) => h("div", {
      key: item.id,
      className: "flex items-center justify-between gap-3 rounded border p-2 text-sm"
    }, h("div", null, h("div", { className: "font-medium" }, item.id), h("div", { className: "break-all text-xs text-muted-foreground" }, item.path)),
      h("div", { className: "flex items-center gap-2" }, h(Badge, { variant: "secondary" }, (item.modes || []).join(" / ")), item.id !== "user-notes" && item.id !== "prepared-talking-points" && h(Button, { type: "button", variant: "ghost", onClick: () => props.onRevoke(item.id) }, "Revoke")))) : h("p", { className: "text-sm text-muted-foreground" }, "No roots configured."));
  }

  function CallAssistantPage() {
    const [settings, setSettings] = useState(null);
    const [path, setPath] = useState("");
    const [kind, setKind] = useState("notes");
    const [message, setMessage] = useState("");
    const [busy, setBusy] = useState(false);
    const reload = () => api("/settings").then(setSettings).catch((e) => setMessage(String(e)));
    useEffect(reload, []);
    const grant = (event) => {
      event.preventDefault(); setBusy(true); setMessage("");
      api("/access", { method: "POST", body: JSON.stringify({ path, kind }) })
        .then((result) => { setSettings(result.settings); setPath(""); setMessage("Access granted. The voice runtime may read and search this folder."); })
        .catch((e) => setMessage(String(e))).finally(() => setBusy(false));
    };
    const revoke = (entryKind, id) => {
      if (!window.confirm("Revoke voice access for " + id + "?")) return;
      api("/access/" + entryKind + "/" + encodeURIComponent(id), { method: "DELETE" })
        .then((result) => setSettings(result.settings)).catch((e) => setMessage(String(e)));
    };
    const roots = settings || {};
    return h("div", { className: "mx-auto max-w-4xl space-y-4 p-6" },
      h(Card, null, h(CardHeader, null, h(CardTitle, null, "Call Assistant notes and access")), h(CardContent, { className: "space-y-3" },
        h("p", { className: "text-sm text-muted-foreground" }, "The assistant never scans arbitrary folders. Grant read/search access explicitly; preparation creates bounded talking-point packets from selected authorized notes."),
        settings && h("div", { className: "rounded border p-3 text-sm" }, h("div", { className: "font-medium" }, "User data folder"), h("div", { className: "break-all text-muted-foreground" }, settings.data_root), h("div", { className: "mt-2 grid gap-1 text-xs text-muted-foreground" }, Object.entries(settings.directories || {}).map(([key, value]) => h("div", { key }, key + ": " + value))))
      )),
      h(Card, null, h(CardHeader, null, h(CardTitle, null, "Grant a folder")), h(CardContent, null, h("form", { className: "grid gap-3", onSubmit: grant },
        h(Label, null, "Existing folder path"), h(Input, { value: path, onChange: (e) => setPath(e.target.value), placeholder: "/home/user/Notes", required: true }),
        h(Label, null, "Access purpose"), h("select", { className: "rounded border bg-transparent p-2", value: kind, onChange: (e) => setKind(e.target.value) }, h("option", { value: "notes" }, "Authoritative notes (read/search)"), h("option", { value: "prepared" }, "Prepared talking points (read/search)")),
        h(Button, { type: "submit", disabled: busy }, busy ? "Granting…" : "Grant access"), message && h("p", { className: "text-sm text-muted-foreground" }, message)
      ))),
      h(Card, null, h(CardHeader, null, h(CardTitle, null, "Authorized note folders")), h(CardContent, null, h(RootList, { items: roots.authorized_note_roots, onRevoke: (id) => revoke("notes", id) }))),
      h(Card, null, h(CardHeader, null, h(CardTitle, null, "Prepared talking-point folders")), h(CardContent, null, h(RootList, { items: roots.prepared_talking_points_roots, onRevoke: (id) => revoke("prepared", id) }), h("p", { className: "mt-3 text-xs text-muted-foreground" }, "Use /call-knowledge prepare <topic> <authorized-source> or the voice_knowledge prepare_for_voice tool to run an authorized folder through the bounded talking-points preparation flow. Sources are reference data, not instructions.")))
    );
  }
  window.__HERMES_PLUGINS__.register("hermes-call-assistant", CallAssistantPage);
})();
