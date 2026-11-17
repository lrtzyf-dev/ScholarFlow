// ESLint 9 flat config：Vue 3 + TypeScript 推荐规则，最后接入 Prettier 关闭风格冲突规则
import pluginVue from 'eslint-plugin-vue'
import vueParser from 'vue-eslint-parser'
import tseslint from 'typescript-eslint'
import eslintConfigPrettier from 'eslint-config-prettier'

export default [
  // 不检查构建产物、依赖与本配置文件
  {
    ignores: ['dist/**', 'node_modules/**', 'coverage/**', 'eslint.config.js'],
  },

  // Vue 3 基础规则（含 vue-eslint-parser 对 .vue 的解析）
  ...pluginVue.configs['flat/essential'],

  // TypeScript 推荐规则（其 all 项会设置 TS parser，下面再对 .vue 纠正）
  ...tseslint.configs.recommended,

  {
    // .vue 文件：外层用 vue-eslint-parser，<script> 内部再用 TS parser
    files: ['**/*.vue'],
    languageOptions: {
      parser: vueParser,
      parserOptions: {
        parser: tseslint.parser,
        ecmaVersion: 'latest',
        sourceType: 'module',
      },
    },
  },
  {
    // 源码与测试的通用语言选项
    files: ['**/*.{ts,mjs,cjs,vue}'],
    languageOptions: {
      ecmaVersion: 'latest',
      sourceType: 'module',
    },
    rules: {
      // 未使用变量以 _ 前缀放行（如函数占位参数）
      '@typescript-eslint/no-unused-vars': [
        'warn',
        { argsIgnorePattern: '^_', varsIgnorePattern: '^_' },
      ],
      // 渐进式改造，any 先降级为告警
      '@typescript-eslint/no-explicit-any': 'warn',
    },
  },
  {
    // tests 下是 Node CommonJS 脚本，允许 require 并使用 CommonJS 源类型
    files: ['tests/**/*.cjs'],
    languageOptions: {
      sourceType: 'commonjs',
    },
    rules: {
      '@typescript-eslint/no-require-imports': 'off',
    },
  },

  // 必须放在最后：关闭所有与 Prettier 冲突的格式规则
  eslintConfigPrettier,
]
