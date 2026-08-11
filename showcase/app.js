const flows = {
  task: {
    eyebrow: "准备上下文",
    title: "先定位上下文，再让 Agent 动手",
    copy: "配置文件限定检索目录，结构索引定位已有文件，正文检索返回带来源的相关段落；长期记忆只保存经过确认的稳定结论。",
    steps: ["限定目录", "定位文件", "检索正文", "打开核对"],
  },
  edit: {
    eyebrow: "更新知识库",
    title: "文件修改后，只同步发生变化的部分",
    copy: "文件哈希定位增删改，临时文件原子替换避免半写入，并在结束时审计文件与 chunk ID 的一致性。",
    steps: ["文件哈希", "更新变化部分", "完整替换索引", "一致性检查"],
  },
  conclude: {
    eyebrow: "保存稳定结论",
    title: "只保存经过确认的稳定结论",
    copy: "只有稳定规则、环境事实和明确偏好进入记忆层。运行事件先写本地 JSONL，外部分析由显式命令按需导出。",
    steps: ["人工确认", "写入稳定结论", "记录本地事件", "按需导出"],
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
