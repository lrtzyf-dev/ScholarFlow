<script setup lang="ts">
import { BookOpen, ShieldCheck, FileCode2, Cpu } from 'lucide-vue-next'
</script>

<template>
  <div class="page">
    <div class="page-head">
      <div>
        <div class="page-title">关于研序</div>
        <div class="page-desc">产品版本、许可证与开源合规声明</div>
      </div>
    </div>

    <div class="card about-card">
      <div class="logo-row">
        <svg viewBox="0 0 24 24" class="logo" fill="none" aria-hidden="true">
          <path d="M4 6.5A2.5 2.5 0 0 1 6.5 4H11v15H6.5A2.5 2.5 0 0 0 4 16.5v-10Z" fill="var(--fn-brand)"/>
          <path d="M20 6.5A2.5 2.5 0 0 0 17.5 4H13v15h4.5a2.5 2.5 0 0 1 2.5 2.5v-15Z" fill="var(--fn-brand)" opacity="0.55"/>
        </svg>
        <div>
          <div class="p-name">研序 ScholarFlow</div>
          <div class="p-ver">版本 1.0.0 · 全流程科研助手</div>
        </div>
      </div>
      <p class="p-desc">
        面向科研工作者的本地优先（local-first）科研助手：PDF 解析、版式阅读、混合检索、
        抗幻觉问答（论断级引用闸门）、引用图谱。数据默认保存在本机，可离线运行。
      </p>
    </div>

    <div class="card">
      <div class="sec-title"><ShieldCheck :size="15"/> 许可证</div>
      <p class="line">本软件为专有软件，版权所有 © 2026 张祎凡。保留所有权利。</p>
      <p class="line">完整的最终用户许可协议见随附的 <code>LICENSE</code> 文件。</p>
    </div>

    <div class="card">
      <div class="sec-title"><FileCode2 :size="15"/> 开源软件声明</div>
      <p class="line">
        本产品的界面设计参考了开源项目 <strong>Octop</strong>（MIT License，Copyright © 2026 Octop），
        其布局与组件经修改后使用，未使用其名称、商标及吉祥物。
      </p>
      <p class="line">
        随附样例使用 <strong>Noto Sans SC</strong>（SIL Open Font License 1.1，
        Copyright © 2014–2021 Adobe），字体名称与版权按许可保留。
      </p>
      <p class="line">
        其余依赖（Vue、FastAPI、pdfplumber、SQLAlchemy、cytoscape、Element Plus 等）
        均采用 MIT / BSD / Apache 2.0 等宽松许可。完整清单见随附 <code>LICENSES.md</code>。
      </p>
    </div>

    <div class="card">
      <div class="sec-title"><Cpu :size="15"/> 运行模式</div>
      <p class="line">
        未配置 API Key 时自动进入离线模式（本地检索 + 示例应答）；
        在「模型与设置」中填入 OpenAI 兼容接口地址与 Key 后即可启用云端大模型，配置热切换。
      </p>
      <p class="line"><BookOpen :size="13" style="vertical-align:-2px"/> 报名/参赛：传智杯 AI 大模型创新应用挑战赛 · 科研助手 Agent 赛道。</p>
    </div>
  </div>
</template>

<style scoped>
.page {
  height: 100%;
  overflow-y: auto;
  padding: 24px 28px;
}
.page-head {
  margin-bottom: 18px;
}
.page-title {
  font-size: 20px;
  font-weight: 700;
  color: var(--fn-text-primary);
}
.page-desc {
  font-size: 13px;
  color: var(--fn-text-tertiary);
  margin-top: 2px;
}
.card {
  background: var(--fn-bg-primary);
  border: 1px solid var(--fn-border-primary);
  border-radius: 12px;
  padding: 16px 18px;
  margin-bottom: 14px;
}
.about-card .logo-row {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 10px;
}
.logo {
  width: 40px;
  height: 40px;
}
.p-name {
  font-size: 16px;
  font-weight: 700;
  color: var(--fn-text-primary);
}
.p-ver {
  font-size: 12px;
  color: var(--fn-text-tertiary);
  margin-top: 2px;
}
.p-desc {
  font-size: 13px;
  color: var(--fn-text-secondary);
  line-height: 1.7;
  margin: 0;
}
.sec-title {
  display: flex;
  align-items: center;
  gap: 7px;
  font-size: 14px;
  font-weight: 600;
  color: var(--fn-text-primary);
  margin-bottom: 10px;
}
.line {
  font-size: 13px;
  color: var(--fn-text-secondary);
  line-height: 1.75;
  margin: 0 0 6px;
}
code {
  background: var(--fn-bg-tertiary);
  padding: 1px 6px;
  border-radius: 5px;
  font-size: 12px;
}
</style>
