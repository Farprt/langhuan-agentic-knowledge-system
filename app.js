const flows = {
  task: {
    eyebrow: "跨目录发现",
    title: "从候选笔记走到当前原文",
    copy: "用 Catalog 找路径，用正文检索发现任意分类下的候选，再选择来源打开当前 Markdown 小节。候选排名提供方向，不替代来源核对。",
    steps: ["描述问题", "找候选", "选择来源", "读原文"],
  },
  edit: {
    eyebrow: "维护检索索引",
    title: "来源变化后再同步对应索引",
    copy: "日常阅读直接使用当前来源；文件发生变化后，增量同步检索索引，并可检查文件与 chunk ID 的一致性。",
    steps: ["编辑 Markdown", "检测变化", "增量同步", "检查索引"],
  },
  conclude: {
    eyebrow: "操作专用上下文",
    title: "受控修改时再查看对应检查",
    copy: "写入、移动或删除等受控操作可用 Task Envelope 返回目标入口和处理检查；普通阅读不需要调用它。",
    steps: ["明确操作", "查看目标入口", "执行相关检查", "核对修改"],
  },
};

const tabs = [...document.querySelectorAll("[data-flow]")];
const panel = document.querySelector("#flow-panel");
const eyebrow = document.querySelector("#flow-eyebrow");
const title = document.querySelector("#flow-title");
const copy = document.querySelector("#flow-copy");
const steps = document.querySelector("#flow-steps");

for (const tab of tabs) {
  tab.addEventListener("click", () => {
    const flow = flows[tab.dataset.flow];
    for (const item of tabs) {
      const selected = item === tab;
      item.classList.toggle("active", selected);
      item.setAttribute("aria-selected", String(selected));
      item.tabIndex = selected ? 0 : -1;
    }
    panel.setAttribute("aria-labelledby", tab.id);
    eyebrow.textContent = flow.eyebrow;
    title.textContent = flow.title;
    copy.textContent = flow.copy;
    steps.replaceChildren(...flow.steps.map((step, index) => {
      const item = document.createElement("li");
      const number = document.createElement("span");
      number.textContent = String(index + 1).padStart(2, "0");
      item.append(number, step);
      return item;
    }));
  });

  tab.addEventListener("keydown", (event) => {
    const current = tabs.indexOf(tab);
    const target = event.key === "ArrowRight"
      ? tabs[(current + 1) % tabs.length]
      : event.key === "ArrowLeft"
        ? tabs[(current - 1 + tabs.length) % tabs.length]
        : event.key === "Home"
          ? tabs[0]
          : event.key === "End"
            ? tabs[tabs.length - 1]
            : null;
    if (target) {
      event.preventDefault();
      target.focus();
      target.click();
    }
  });
}

const copyButton = document.querySelector("#copy-quickstart");
const copyStatus = document.querySelector("#copy-status");
copyButton.addEventListener("click", async () => {
  try {
    await navigator.clipboard.writeText(document.querySelector("#quickstart-code").textContent);
    copyButton.textContent = "已复制";
    copyStatus.textContent = "命令已复制到剪贴板。";
    window.setTimeout(() => {
      copyButton.textContent = "复制";
      copyStatus.textContent = "";
    }, 1800);
  } catch {
    copyStatus.textContent = "浏览器未授予剪贴板权限，请手动选择命令。";
  }
});
