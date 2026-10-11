# WP05B 工况研究契约

属于 integrated-review-release，继承其全部约束；仅软件研究/影子决策，不作现场认证。

## 输入和纯函数

`backend.studies.condition_models.train_conditions(payload: dict) -> dict`，版本 `condition_bp/1`。

训练 payload 仅允许：`schema='condition.training/1'`, `features`, `rows`, `rules`, `options`。

- features: 1—16 个 `{name,unit}`，唯一非空 name<=64，unit<=24。不得用 well/cycle/label/id/time 等身份字段做 feature；数值矩阵由 rows.values 显式提供。
- rows: 18—2048 个 `{sample_id,well_id,cycle_id,observed_at,partition,label,values}`。sample_id/cycle_id 全局唯一非空<=128；well_id<=128；时间必须带时区；partition 仅 train/validation/test；label非空<=64，2—32类。values 长度与 features 完全相同，有限、非 bool，绝对值<=1e12。
- train >=6，validation/test 各>=6，各分区包含全部训练类别。井的集合三分区互斥；最大 train UTC 时间 < 最小 validation 时间且最大 validation < 最小 test；不同分区重复周期/样本直接拒绝，不自动重新切分。每类 train 至少2个。
- rules: 1—32 项 `{label,clauses}`；clauses 1—8 个 `{feature,op,value}`，feature须存在，op仅 le/ge，value有限界同上。多个规则同时命中不同 label 时为 unknown；没有命中也 unknown。规则是分析人声明的基线，不自动把测试标签转换成规则。
- options 仅 `{seed,hidden_units,epochs,learning_rate,l2,confidence_min,ood_margin}`，均显式提供。seed整数0..2^32-1；hidden_units1..32；epochs1..300；learning_rate(0,1]；l2[0,1]；confidence_min[0.5,0.999]；ood_margin[0,2]。`len(train)*features*hidden_units*epochs*(1+classes/features)` 的保守运算估算不得超过 `20_000_000`；超限报资源上限，不静默缩数据/轮次。

所有对象拒绝未知键，数值拒绝 bool/NaN/Inf；字符串拒绝控制字符。使用 `backend.studies.contracts.StudyError`，若共享模块未就绪，等待接口而不建立不兼容错误类型。

## 算法

train-only mean/std 标准化，常量维度变为0并保留常量标记。确定性本地 random.Random(seed) 初始化单隐层 tanh、softmax 多分类网络；逐轮全批量梯度下降、交叉熵 + L2，记录训练/验证损失；选择验证损失最小的已训练权重（不看测试集选轮次）。输出已执行轮次、最佳轮次和停止原因；梯度/权重/损失任何非有限即拒绝。

统计基线是训练集标准化后的每类中心，欧氏距离最近类；平局按 label 字典序但显式报告 tie。规则、中心、BP 同一测试集。OOD 判断按每维 train min/max 外扩 `ood_margin*max(range,abs(mean)*1e-12,1e-12)`；超范围为 unknown；训练常量不因很小浮动造成除零。BP max softmax < confidence_min 亦 unknown；称“未校准模型分数”，不称病理概率。

每种方法给 confusion_matrix（标签行，预测类别及unknown列）、每类precision/recall/F1、macro_F1（缺预测分母0则0）、accepted_count/unknown_count/coverage、all-row accuracy 和 accepted-only accuracy（无接受时null），以及每条测试预测/真实label/原因。记录每分区井数、样本数、时间段、每类数量，数据摘要、schema、algorithm_version、训练损失和特征范围。实际测试集没出现的条件不得推断准确率。

## 数值制品和推理

artifact 仅 JSON：`schema='condition.artifact/1'`, `algorithm_version='condition_bp/1'`, `features`, `labels`, `scaler`（mean/std/min/max/constant）, `hidden_units`, `weights_input`, `bias_hidden`, `weights_output`, `bias_output`, `training_digest`, `source_scope='research_only'`。精确维度、数量/数值界检查；禁止 pickle、动态导入或路径。制品包含规则/类中心可选为独立baseline字段，但不得增加执行语言。

`predict_conditions(payload:dict)->dict` 内部输入仅 `{schema='condition.prediction/1',model_record_id,artifact,rows,confidence_min,ood_margin}`。公开HTTP不接受artifact，服务从当前批准且影子选用的训练记录解析并插入。rows1—2048，结构 `{sample_id,well_id,cycle_id,observed_at,values}`，IDs唯一、时间合法；新行也不能匹配训练/验证/测试身份而被称独立验证（推理不输出准确率）。返回status='completed'、逐样本 label或unknown、score、ood_features/reasons、输入摘要、artifact摘要、`field_validation=false`,`control_eligible=false`。制品源与上游复核/选用由持久化服务复核，純函数不访问数据库。

训练返回 `status='completed'`, `artifact`, `metrics`（rule/centroid/bp）, `partitions`, `training_curve`, `best_epoch`, `warnings`, `field_validation=false`, `control_eligible=false`。

## 必测行为

独立手算混淆矩阵；标签/井/时间/周期泄漏拒绝；测试集极端值不改变train scaler；学习非线性小型问题的损失下降；权重随训练变化，不是随机/硬编码预测；同seed同输出；test标签改动只改评估不改权重；常量维度；unknown/低置信/OOD；恶意/错维制品和超限训练拒绝。合成例子不能用作真实准确率。

## 方法依据

- <https://scikit-learn.org/stable/modules/neural_networks_supervised.html>：MLP/反向传播与特征缩放。
- <https://scikit-learn.org/stable/common_pitfalls.html>：训练专属预处理与数据泄漏防范。
- <https://scikit-learn.org/stable/model_persistence.html>：不加载不可信 pickle；本项目限定数值 JSON。

本实现采用有限标准库基线，不宣称已训练行业通用工况模型或达到现场识别精度。
