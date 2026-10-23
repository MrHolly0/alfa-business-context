import React, { useState, useEffect, useRef } from "react";
import { createRoot } from "react-dom/client";
import { flushSync } from "react-dom";
import parse, { domToReact, attributesToProps } from "html-react-parser";
import { Button } from "@alfalab/core-components/button/esm";
import { Input } from "@alfalab/core-components/input/esm";
import { Textarea } from "@alfalab/core-components/textarea/esm";
import { SelectDesktop } from "@alfalab/core-components/select/esm/desktop";
import { Typography } from "@alfalab/core-components/typography/esm";
import "@alfalab/core-components-vars/common.css";
import "@alfalab/core-components-vars/border-radius.css";
import "@alfalab/core-components-vars/gaps.css";
import "@alfalab/core-components-vars/typography-vars.css";
import "@alfalab/core-components-vars/colors-bluetint.css";
import "@alfalab/core-components-themes/corp.css";

const roots = new WeakMap();
const elements = (node) =>
  (node.children || []).filter((n) => n.type === "tag");
const classHas = (node, value) =>
  (node.attribs?.class || "").split(" ").includes(value);
const content = (node) =>
  (node.children || [])
    .map((n) => (n.type === "text" ? n.data : content(n)))
    .join("");

function CoreSelectField({ node }) {
  const nodes = elements(node),
    field = nodes.find((n) => n.name === "input"),
    trigger = nodes.find((n) => n.name === "button");
  const attrs = field.attribs,
    options = JSON.parse(trigger.attribs["data-options"]).map(
      ([key, content]) => ({ key, content }),
    );
  const label = trigger.attribs["aria-label"],
    [value, setValue] = useState(attrs.value || "");
  const hidden = useRef(null);
  useEffect(() => setValue(attrs.value || ""), [attrs.value]);
  return (
    <div className={node.attribs.class}>
      <input
        ref={hidden}
        type="hidden"
        id={attrs.id}
        name={attrs.name}
        value={value}
      />
      <SelectDesktop
        className="core-select"
        id={"core-" + attrs.id}
        label={label}
        labelView="outer"
        options={options}
        selected={value}
        size={48}
        block
        optionsListMaxHeight={320}
        onChange={({ selected }) => {
          const next = selected?.key || "";
          setValue(next);
          if (hidden.current) {
            hidden.current.value = next;
            hidden.current.dispatchEvent(
              new Event("change", { bubbles: true }),
            );
          }
        }}
      />
      {nodes
        .filter((n) => classHas(n, "hint"))
        .map((n, i) => (
          <span className="hint" key={i}>
            {content(n)}
          </span>
        ))}
    </div>
  );
}
function CoreTextField({ node, search = false }) {
  const nodes = elements(node),
    target = nodes.find((n) => n.name === "input" || n.name === "textarea"),
    attrs = target.attribs;
  const [value, setValue] = useState(
    attrs.value ?? (target.name === "textarea" ? content(target) : ""),
  );
  useEffect(
    () =>
      setValue(
        attrs.value ?? (target.name === "textarea" ? content(target) : ""),
      ),
    [attrs.value, content(target)],
  );
  const props = attributesToProps(attrs);
  delete props.value;
  delete props.className;
  const label = search
    ? undefined
    : content(
        nodes.find((n) => n.name === "span" && !classHas(n, "hint")) || {},
      );
  const hint = nodes.find((n) => classHas(n, "hint"));
  const Component = target.name === "textarea" ? Textarea : Input;
  return (
    <Component
      {...props}
      className={search ? "core-search" : node.attribs.class}
      label={search ? attrs["aria-label"] || attrs.placeholder : label}
      labelClassName={search ? "sr-only" : undefined}
      labelView="outer"
      block
      size={48}
      value={value}
      hint={hint ? content(hint) : undefined}
      leftAddons={
        search
          ? domToReact(
              nodes.filter((n) => n.name === "svg"),
              options,
            )
          : undefined
      }
      onChange={(e) => setValue(e.target.value)}
    />
  );
}
const options = {
  replace(node) {
    if (node.type !== "tag") return;
    if (node.name === "label" && classHas(node, "field")) {
      const nodes = elements(node);
      if (
        nodes.some(
          (n) => n.name === "button" && n.attribs["data-ui"] === "select",
        )
      )
        return <CoreSelectField node={node} />;
      if (
        nodes.some(
          (n) =>
            (n.name === "input" && n.attribs.type !== "hidden") ||
            n.name === "textarea",
        )
      )
        return <CoreTextField node={node} />;
    }
    if (node.name === "label" && classHas(node, "search"))
      return <CoreTextField node={node} search />;
    if (
      node.name === "button" &&
      classHas(node, "btn") &&
      ["primary", "secondary", "quiet", "text", "danger"].some((c) =>
        classHas(node, c),
      )
    ) {
      const props = attributesToProps(node.attribs),
        cls = props.className;
      delete props.className;
      const view = classHas(node, "primary")
        ? "primary"
        : classHas(node, "quiet")
          ? "outlined"
          : classHas(node, "text")
            ? "text"
            : "secondary";
      const addon = node.children.find((n) => n.name === "svg");
      return (
        <Button
          {...props}
          className={
            "core-action " +
            (classHas(node.parent || {}, "toolbar-tools")
              ? "core-toolbar-action "
              : "") +
            cls
              .split(" ")
              .filter((c) => c !== "btn")
              .join(" ")
          }
          view={view}
          leftAddons={
            addon && props["data-icon-position"] !== "right" ? (
              <span className="core-button-icon">
                {domToReact([addon], options)}
              </span>
            ) : undefined
          }
          rightAddons={addon && props["data-icon-position"] === "right" ? <span className="core-button-icon">{domToReact([addon], options)}</span> : undefined}
          size={48}
        >
          <span className="core-button-label">
            {domToReact(
              node.children.filter((n) => n !== addon),
              options,
            )}
          </span>
        </Button>
      );
    }
    if (["h1", "h2", "h3"].includes(node.name)) {
      const props = attributesToProps(node.attribs);
      return (
        <Typography.Title
          {...props}
          font="system"
          tag={node.name}
          defaultMargins={false}
          view={
            node.name === "h1"
              ? "medium"
              : node.name === "h2"
                ? "small"
                : "xsmall"
          }
          className={(props.className || "") + " core-heading"}
        >
          {domToReact(node.children, options)}
        </Typography.Title>
      );
    }
  },
};
export function renderMarkup(element, html) {
  let root = roots.get(element);
  if (!root) {
    root = createRoot(element);
    roots.set(element, root);
  }
  flushSync(() => root.render(parse(html, options)));
}
