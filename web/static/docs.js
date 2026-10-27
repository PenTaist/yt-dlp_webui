"use strict";

document.querySelectorAll(".origin").forEach((el) => { el.textContent = window.location.origin; });

document.querySelectorAll("pre").forEach((pre) => {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "copy";
  button.textContent = "Copier";
  button.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(pre.querySelector("code").textContent);
      button.textContent = "Copié";
    } catch {
      button.textContent = "Copie impossible";
    }
    setTimeout(() => { button.textContent = "Copier"; }, 1600);
  });
  const wrap = document.createElement("div");
  wrap.className = "code-wrap";
  pre.replaceWith(wrap);
  wrap.append(pre, button);
});
