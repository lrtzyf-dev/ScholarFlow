<script setup lang="ts">
import { BookOpen, ShieldCheck, FileCode2, Cpu } from 'lucide-vue-next'
import { useRouter } from 'vue-router'
import ReleaseInfo from '../components/ReleaseInfo.vue'
const router = useRouter()

</script>

<template>
  <div class="page">
    <div class="page-head">
      <div>
        <h1 class="page-title">关于研序</h1>
        <div class="page-desc">ScholarFlow · 科研 Agent 工作空间</div>
      </div>
    </div>

    <div class="card about-card">
      <div class="logo-row">
        <img class="about-mark" src="/app-icon.png" alt="研序 ScholarFlow 图标" />
        <div>
          <div class="p-name">研序 ScholarFlow</div>
          <div class="p-ver">整理文献、核验引用，围绕研究问题持续工作。</div>
          <span class="about-tag">本地科研工作台</span>
        </div>
      </div>
    </div>
    <ReleaseInfo />
    <section class="card delivery-section">
      <h2 class="sec-title">当前版本</h2>
      <p class="line">
        覆盖问答、文献、分析、写作和系统设置。论文、解析结果与研究记录保存在本机；连接你自己的模型服务后，即可围绕真实文献开展研究。
      </p>
      <div class="about-actions">
        <button class="about-btn primary" @click="router.push('/library')">
          <BookOpen :size="16" />打开文献库
        </button>
        <button class="about-btn" @click="router.push('/chat')">开始研究</button>
        <button class="about-btn" @click="router.push('/settings')">模型设置</button>
      </div>
    </section>


    <details class="license-details">
      <summary>许可证与开源软件声明</summary>
      <div class="card">
        <div class="sec-title"><ShieldCheck :size="15" /> 许可证</div>
        <p class="line">研序由张祎凡独立开发，项目原创代码采用 MIT License，著作权归作者所有。</p>
        <p class="line">授权范围与条件见根目录 <code>LICENSE</code> 文件。</p>
      </div>

      <div class="card">
        <div class="sec-title"><FileCode2 :size="15" /> 组件许可</div>
        <p class="line">第三方依赖按其原有许可使用，详情见根目录 <code>LICENSES.md</code>。</p>
      </div>
    </details>

    <div class="card">
      <div class="sec-title"><Cpu :size="15" /> 使用说明</div>
      <p class="line">
        新安装默认选择 deepseek-flash，云模型缺少密钥时保持待配置。可在会话口或设置中添加其他兼容接口或真实本地模型；离线演示需要主动选择，并始终标注为示例响应。
      </p>
      <p class="line">
        <BookOpen :size="13" style="vertical-align: -2px" /> 开发环境、功能范围与数据安全说明见项目仓库 README。
      </p>
    </div>
  </div>
</template>

<style scoped>
.page {
  font-size: 14px;
  line-height: 1.6;
  height: 100%;
  overflow-y: auto;
  width: 100%;
  max-width: 930px;
  margin: 0 auto;
  padding: 38px 42px 28px;
  background: var(--fn-bg-primary);
}
.page-head {
  margin-bottom: 29px;
}
.page-title {
  margin: 0 0 8px;
  font-size: 24px;
  line-height: 1.4;
  font-weight: 600;
  letter-spacing: 0;
  color: var(--fn-text-primary);
}
.page-desc {
  font-size: 13px;
  color: var(--muted);
  margin-top: 0;
  line-height: 1.8;
}
.card {
  background: var(--fn-bg-primary);
  border: 0;
  border-bottom: 1px solid var(--fn-border-primary);
  border-radius: 0;
  box-shadow: none;
  padding: 28px 0;
  margin: 0;
}
.about-card .logo-row {
  display: flex;
  align-items: center;
  gap: 22px;
  margin-bottom: 0;
}
.logo {
  width: 40px;
  height: 40px;
}
.p-name {
  font-size: 21px;
  font-weight: 500;
  letter-spacing: -0.5px;
  color: var(--fn-text-primary);
}
.p-ver {
  font-size: 12px;
  color: var(--fn-text-tertiary);
  margin: 6px 0 11px;
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
  font-weight: 500;
  color: var(--fn-text-primary);
  margin: 0 0 15px;
}
.line {
  font-size: 12px;
  color: var(--muted);
  line-height: 1.9;
  margin: 0 0 6px;
}
code {
  background: var(--fn-bg-tertiary);
  padding: 1px 6px;
  border-radius: 5px;
  font-size: 12px;
}
.about-card {
  padding: 20px 0 34px;
}
.about-mark {
  object-fit: contain;
  display: grid;
  place-items: center;
  width: 66px;
  height: 66px;
  flex-shrink: 0;
  border: 1px solid var(--fn-border-primary);
  border-radius: 8px;
  background: var(--fn-bg-secondary);
  color: var(--text);
  font:
    28px/1 'Songti SC',
    SimSun,
    serif;
}
.p-desc {
  max-width: 680px;
}
.card:last-child {
  border-bottom: 0;
}
@media (max-width: 1100px) {
  .page {
    padding: 30px 28px;
  }
}
@media (max-width: 760px) {
  .page {
    padding: 25px 20px;
  }
  .about-card .logo-row {
    gap: 17px;
  }
  .about-mark {
    object-fit: contain;
    width: 54px;
    height: 54px;
  }
  .p-name {
    font-size: 18px;
  }
}

.about-tag {
  display: inline-flex;
  padding: 3px 7px;
  border: 1px solid var(--border);
  border-radius: 4px;
  background: var(--subtle);
  color: var(--muted);
  font-size: 10px;
}
.about-actions {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 20px;
  flex-wrap: wrap;
}
.about-btn {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  min-height: 36px;
  padding: 7px 13px;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--surface);
  color: var(--text);
  font: inherit;
  font-size: 13px;
  font-weight: 500;
}
.about-btn.primary {
  background: var(--primary);
  border-color: var(--primary);
  color: var(--on-primary);
}
.license-details {
  border-bottom: 1px solid var(--border);
  padding: 18px 0;
}
.license-details summary {
  cursor: pointer;
  color: var(--muted);
  font-size: 12px;
}
.license-details .card:last-child {
  border-bottom: 0;
  padding-bottom: 0;
}
@media (max-width: 760px) {
  .about-mark {
    object-fit: contain;
    font-size: 24px;
  }
}
</style>
