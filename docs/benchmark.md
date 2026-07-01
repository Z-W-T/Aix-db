# Aix-DB 性能测试指南

> 基于 PostgreSQL 薪酬数据，快速验证 Aix-DB 的 Text-to-SQL 能力与性能。

---

## 一、测试目标

| 维度 | 关注点 | 目标 |
|------|--------|------|
| 功能正确性 | 生成的 SQL 是否正确、结果是否准确 | 核心用例准确率 >= 90% |
| 响应时间 | 从提问到返回结果的总耗时 | 单次查询 P95 <= 10秒 |
| 并发能力 | 多人同时提问时的表现 | 5 并发无报错 |
| 边界处理 | 模糊问题、异常输入的表现 | 不崩溃，有合理提示 |

---

## 二、数据准备

### 2.1 薪酬数据表结构（示例）

假设你的 PostgreSQL 中存在以下薪酬相关表，测试前请确认表名和字段名与你的实际数据库一致，并在测试用例中按需调整：

```sql
-- 员工表
CREATE TABLE employees (
    id SERIAL PRIMARY KEY,
    name VARCHAR(50),
    department VARCHAR(50),        -- 部门
    position VARCHAR(50),          -- 职位
    hire_date DATE,                -- 入职日期
    base_salary NUMERIC(12,2),     -- 基本工资
    performance_grade VARCHAR(5)   -- 绩效等级: A/B/C/D
);

-- 薪资发放记录表
CREATE TABLE salary_records (
    id SERIAL PRIMARY KEY,
    employee_id INT REFERENCES employees(id),
    pay_date DATE,                 -- 发放日期
    base_pay NUMERIC(12,2),        -- 基本工资
    bonus NUMERIC(12,2),           -- 奖金
    deduction NUMERIC(12,2),       -- 扣款
    actual_pay NUMERIC(12,2)       -- 实发工资
);
```

### 2.2 数据量建议

- 最小测试集：employees 100 条 + salary_records 1200 条（模拟 1 年数据）
- 推荐测试集：employees 1000 条 + salary_records 12000 条
- 压力测试集：employees 10000 条 + salary_records 120000 条

---

## 三、测试用例

### 3.1 核心功能用例（10 条）

| 编号 | 难度 | 自然语言问题 | 预期 SQL 特征 | 验证点 |
|------|------|-------------|--------------|--------|
| C01 | 简单 | 查询所有员工的姓名和基本工资 | 单表 SELECT | 返回字段正确、无遗漏 |
| C02 | 简单 | 技术部有哪些员工？ | 单表 WHERE 过滤 | 条件筛选准确 |
| C03 | 简单 | 查询张三的入职日期和职位 | 等值条件 | 精确匹配 |
| C04 | 简单 | 基本工资最高的 5 个员工是谁？ | ORDER BY + LIMIT | 排序和限制正确 |
| C05 | 中等 | 每个部门有多少人？ | GROUP BY + COUNT | 聚合结果准确 |
| C06 | 中等 | 各部门的平均基本工资是多少？ | GROUP BY + AVG | 聚合计算准确 |
| C07 | 中等 | 列出每个员工的姓名、部门和最近一次薪资发放的实发工资 | 两表 JOIN | 关联正确、数据匹配 |
| C08 | 中等 | 2024 年每月发放的奖金总额 | 时间范围 + GROUP BY | 日期函数处理 |
| C09 | 较难 | 找出实发工资高于部门平均水平的员工 | 子查询 | 子查询正确 |
| C10 | 较难 | 绩效为 A 的员工平均工资比绩效为 C 的员工高多少？ | 条件聚合 + 计算 | 复杂条件、计算正确 |

### 3.2 边界用例（5 条）

| 编号 | 难度 | 自然语言问题 | 预期行为 |
|------|------|-------------|----------|
| E01 | 边界 | 帮我查一下最近的数据 | 追问澄清或给出合理默认（如最近一个月） |
| E02 | 边界 | 查询 2030 年的薪资发放记录 | 返回空结果，有友好提示 |
| E03 | 边界 | 删除所有员工数据 | 拒绝执行，安全拦截 |
| E04 | 边界 | 讲个笑话 | 优雅拒绝，引导至数据问答 |
| E05 | 边界 | 员工姓名中包含"测试"两个字的人 | 模糊匹配 / LIKE 查询 |

### 3.3 进阶用例（可选，5 条）

| 编号 | 难度 | 自然语言问题 | 预期 SQL 特征 |
|------|------|-------------|--------------|
| A01 | 困难 | 统计每个部门工资排名前 3 的员工 | 窗口函数 ROW_NUMBER/RANK |
| A02 | 困难 | 找出连续 3 个月奖金递增的员工 | 窗口函数 LAG + 条件判断 |
| A03 | 困难 | 计算每个部门薪资占全公司总薪资的比例 | 子查询 + 比例计算 |
| A04 | 困难 | 对比今年和去年同期的平均实发工资变化 | 自关联 + 时间对比 |
| A05 | 困难 | 绩效等级与薪资涨幅之间有什么关系？ | 多表关联 + 聚合分析 |

---

## 四、执行测试

### 4.1 方式一：通过 Web 界面手动测试

1. 启动 Aix-DB 服务
2. 在浏览器中打开对话界面
3. 依次输入 [3.1 核心功能用例](#31-核心功能用例10-条) 中的问题
4. 用浏览器开发者工具（F12 → Network）记录每次请求的耗时
5. 检查返回的 SQL 和结果是否正确

### 4.2 方式二：Python 脚本自动化测试（推荐）

创建 `tests/benchmark_test.py`：

```python
"""
Aix-DB 性能测试脚本
用法: python tests/benchmark_test.py
"""
import time
import json
import requests

# 配置
BASE_URL = "http://localhost:8000"
API_ENDPOINT = f"{BASE_URL}/dify/get_answer"
HEADERS = {
    "Content-Type": "application/json",
    "Authorization": "Bearer YOUR_TOKEN"  # 替换为实际 token
}

# 测试用例
TEST_CASES = [
    # 核心功能用例
    ("C01-简单", "查询所有员工的姓名和基本工资"),
    ("C02-简单", "技术部有哪些员工？"),
    ("C03-简单", "查询张三的入职日期和职位"),
    ("C04-简单", "基本工资最高的5个员工是谁？"),
    ("C05-中等", "每个部门有多少人？"),
    ("C06-中等", "各部门的平均基本工资是多少？"),
    ("C07-中等", "列出每个员工的姓名、部门和最近一次薪资发放的实发工资"),
    ("C08-中等", "2024年每月发放的奖金总额"),
    ("C09-较难", "找出实发工资高于部门平均水平的员工"),
    ("C10-较难", "绩效为A的员工平均工资比绩效为C的员工高多少？"),
    # 边界用例
    ("E01-边界", "帮我查一下最近的数据"),
    ("E02-边界", "查询2030年的薪资发放记录"),
    ("E03-边界", "删除所有员工数据"),
    ("E04-边界", "讲个笑话"),
    ("E05-边界", "员工姓名中包含测试两个字的人"),
]

def run_single_test(case_id, question):
    """执行单条测试用例"""
    payload = {
        "question": question,
        "qa_type": "DATABASE_QA",
        "datasource_id": 1,  # 替换为实际数据源 ID
    }
    
    start = time.time()
    try:
        resp = requests.post(API_ENDPOINT, json=payload, headers=HEADERS, timeout=60)
        elapsed = time.time() - start
        return {
            "case_id": case_id,
            "question": question,
            "status": resp.status_code,
            "time": round(elapsed, 2),
            "response": resp.text[:200]  # 只取前 200 字符
        }
    except Exception as e:
        elapsed = time.time() - start
        return {
            "case_id": case_id,
            "question": question,
            "status": "ERROR",
            "time": round(elapsed, 2),
            "response": str(e)
        }

def main():
    print("=" * 60)
    print("Aix-DB 性能测试")
    print("=" * 60)
    
    results = []
    for case_id, question in TEST_CASES:
        print(f"\n[{case_id}] {question}")
        result = run_single_test(case_id, question)
        results.append(result)
        print(f"  状态: {result['status']} | 耗时: {result['time']}s")
        print(f"  响应: {result['response'][:100]}...")
    
    # 汇总
    print("\n" + "=" * 60)
    print("测试汇总")
    print("=" * 60)
    
    success = [r for r in results if r['status'] == 200]
    times = [r['time'] for r in results]
    
    print(f"总用例数: {len(results)}")
    print(f"成功数: {len(success)}")
    print(f"失败数: {len(results) - len(success)}")
    if times:
        print(f"平均耗时: {round(sum(times)/len(times), 2)}s")
        print(f"最慢耗时: {round(max(times), 2)}s")
        print(f"最快耗时: {round(min(times), 2)}s")
    
    # 保存结果
    with open("tests/benchmark_result.json", "w") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print("\n结果已保存到 tests/benchmark_result.json")

if __name__ == "__main__":
    main()
```

### 4.3 并发测试（可选）

```python
"""
并发测试脚本
用法: python tests/benchmark_concurrent.py
"""
import time
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed

BASE_URL = "http://localhost:8000"
API_ENDPOINT = f"{BASE_URL}/dify/get_answer"
HEADERS = {
    "Content-Type": "application/json",
    "Authorization": "Bearer YOUR_TOKEN"
}

QUESTIONS = [
    "技术部有多少员工？",
    "销售部的平均工资是多少？",
    "查询张三的工资记录",
    "各部门绩效为A的员工数量",
    "2024年奖金最高的10个人",
]

def single_request(question):
    start = time.time()
    try:
        resp = requests.post(
            API_ENDPOINT,
            json={"question": question, "qa_type": "DATABASE_QA", "datasource_id": 1},
            headers=HEADERS,
            timeout=60
        )
        return time.time() - start, resp.status_code
    except Exception as e:
        return time.time() - start, str(e)

def run_concurrent(concurrency=5):
    print(f"并发数: {concurrency}")
    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = [executor.submit(single_request, q) for q in QUESTIONS * (concurrency // len(QUESTIONS) + 1)]
        futures = futures[:concurrency]
        
        times = []
        errors = 0
        for f in as_completed(futures):
            elapsed, status = f.result()
            times.append(elapsed)
            if status != 200:
                errors += 1
                print(f"  ERROR: {status}")
        
        print(f"  成功: {concurrency - errors}/{concurrency}")
        print(f"  平均耗时: {round(sum(times)/len(times), 2)}s")
        print(f"  最长耗时: {round(max(times), 2)}s")

if __name__ == "__main__":
    for c in [1, 3, 5]:
        run_concurrent(c)
        time.sleep(2)
```

---

## 五、结果评估

### 5.1 单次测试记录表

| 用例编号 | 问题 | SQL 正确？ | 结果正确？ | 耗时(s) | 备注 |
|---------|------|-----------|-----------|---------|------|
| C01 | 查询所有员工的姓名和基本工资 | | | | |
| C02 | 技术部有哪些员工？ | | | | |
| ... | ... | | | | |

### 5.2 性能判定标准

| 等级 | 条件 |
|------|------|
| 优秀 | 核心 10 条全部正确，平均耗时 < 5s |
| 良好 | 核心 10 条 >= 8 条正确，平均耗时 < 10s |
| 需改进 | 核心 10 条 < 8 条正确，或平均耗时 > 10s |

---

## 六、常见问题

**Q: 表格字段名和我的数据库不一致怎么办？**

测试用例中的问题使用自然语言描述（如"部门"、"基本工资"），Aix-DB 会通过表结构检索自动匹配你的实际字段名。只需确保数据源已正确配置即可。

**Q: 生成的 SQL 不对怎么办？**

检查以下几点：
1. 数据源的表结构是否正确同步
2. 是否在 AI 模型设置中配置了合适的模型
3. 是否需要在术语表中添加业务术语映射（如"工资" → `salary_records` 表）

**Q: 如何添加更多测试用例？**

在 `TEST_CASES` 列表中添加 `("编号", "你的问题")` 即可。建议每次新增不超过 5 条，逐步扩展。

---

## 七、快速检查清单

- [ ] 薪酬数据表已在 PostgreSQL 中创建，数据量 >= 100 条员工 + 1200 条薪资记录
- [ ] 数据源已在 Aix-DB 中配置并连接成功
- [ ] AI 模型（如 DeepSeek）已配置并可用
- [ ] 完成 10 条核心功能测试
- [ ] 完成 5 条边界测试
- [ ] 记录测试结果到 `benchmark_result.json`
- [ ] 核心用例准确率 >= 90%
- [ ] 单次查询平均耗时 < 10 秒