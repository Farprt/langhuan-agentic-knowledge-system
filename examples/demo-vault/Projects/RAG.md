---
title: 本地优先 RAG
type: project
area: Knowledge Engineering
visibility: public
---

# 本地优先 RAG

## 检索流水线

系统组合确定性 Dense Retrieval、BM25 与 Reciprocal Rank Fusion，并在分块中保留标题上下文。

## 增量同步

文件哈希用于发现变化；写入完成后检查文件、知识块与向量的一致性。

## 隐私边界

索引和事件默认只保存在本机。事件不记录查询正文，任何云端导出都必须显式配置。
