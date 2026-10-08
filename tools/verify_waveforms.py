"""Owned WP04 verification. Local synthetic evidence is not deployment approval."""
import argparse
from collections import Counter
from datetime import datetime, timezone
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest
from uuid import uuid4

ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path: sys.path.insert(0,str(ROOT))
SCENARIOS=frozenset(('online_then_offline','offline_then_online','transport_judgments',
    'receipt_chain','gateway_isolation','lease_recovery','sender_storage','sender_maximum',
    'raw_maximum','summary_maximum','xy','audit_volume','dataset_1000','output_owners'))
RESTORE_CHECKS=('snapshot','content','schema','acl','extension','exports','restrictions')
RESOURCE_CONTINUATION_PATHS=frozenset(('tools/verify_waveforms.py','tests/waveform_experiments.py',
    'tests/test_waveform_verification_tool.py','tests/test_waveform_end_to_end.py'))
PROTOCOL_SCENARIOS=frozenset(('online_then_offline','offline_then_online','transport_judgments','receipt_chain','gateway_isolation','lease_recovery'))
RESOURCE_SCENARIOS=SCENARIOS-PROTOCOL_SCENARIOS-{'dataset_1000'}
CONTINUATION_PATHS=frozenset(('tools/verify_waveforms.py','tests/waveform_experiments.py',
    'tests/waveform_measurements.py','tests/waveform_resource_scenarios.py',
    'tests/test_waveform_verification_tool.py','tests/test_waveform_end_to_end.py','tests/test_waveform_failure_recovery.py'))
R78_PRODUCT_DELTA={
    'backend/waveforms/windows.py':dict(before='7f74877a8c9ec21daf9ef9c2df6816b582af388629704a7b4b3a5a63dd2ad3c8',after='590ac7ead178ffa6c3dd2467125ca6a963a9050a5dbd028050aa3438ff82af97'),
    'tests/test_waveform_windows.py':dict(before='b4d037b33c011e4317ee90b72234799293e64ff24c109c61a639d9bbd0d1343a',after='a88d29cce12e861b8dbd3755c6729648cb47a205a24ec818e1603c6a0a3d2112')}
R82_RECOVERY_DELTA={
    'backend/waveforms/migrate.py':dict(before='52aac20f908b004a20d1f31c73d05f47f5019be90e35696d728c6e038a7ca5e5',after='91a101f025f82cf90ab762105083340a5f5176609eb18ef4795171798047f538'),
    'backend/waveforms/sql/005_portable_structure.sql':dict(before=None,after='4e8cdb6f00099ecbb76eb2fa37765654f3317d3b073ff61f5e01d1ed3d524bda'),
    'tools/waveform_restore.py':dict(before='1fe9b99f68034e7f2251920bb4e280958d82d6bb523118cab38c4d6e081a4ec8',after='9bac5a861ef5687e10c34376d4fc0497d184219f47252a5bed16b2a3e0a5b1f3'),
    'tests/test_waveform_schema.py':dict(before='6688ae605b2c934f31a91323c6baa897a9f59438d667f48ee825bbff923fab0d',after='d5dcff6967e6abda918374f297fe59535073ccd1ee225447a852d2d10110043e'),
    'tests/test_waveform_dataset_upgrade.py':dict(before='2e814e4198c7fe01d433b076f5821bf9f0c3ac65c507152c428fc662d496f048',after='f08163dce5cfc6d400af5bb12794248a4563f7516b3d1a03603be50f3ca34825')}



# Reviewed, fixed checkpoint deltas. Ignored inventories are NOT runtime inputs.
RUN1_REPORT_SHA256 = '05176216f12af6d99294942ce6ceb477f271c9e02571ab5c618578f43eafe1b9'
RUN1_ID = 'd0bd49eefb4c4244b10d7c054b088265'
RUN3_REPORT_SHA256 = '68c5ea635fc8c191fcb6f1474f233038a3bdb69d63c95c39cce6f56bed5f152e'
RUN3_ID = '0ab142af1ff2451baf3bf52abc5ca802'
RUN3_SOURCE_SHA256 = '9b26d226c5799bdbdf0043e986c086e9b2675c03ae5ecbc936e859dfef9d2121'
RETAINED_BASE_SHA256='1e8680b3e3cd7e3b22c2f9118bcce896ce3b127b4ad93c643823f2bad2b5bffd'
RETAINED_DIAGNOSTIC_DELTA={
    'tests/waveform_measurements.py':dict(before='cdd1dbbf220c4d23abe74fe0e3032a3dfb314511673116a347cf171c74782d0e',after='502596b206b11f4e9e9fe1fcf23921b345c4434b9162e61ef4374fe50acef4ec'),
    'tests/waveform_resource_scenarios.py':dict(before='e06c60d15400a2d9708fb07909cb8b3d570e7eeb5108d8da8e6f2bbc9eb7c370',after='fe4bc58039e48f9999ba24dbbbd3bf9e70e76059b497b61277842ae2c31befec'),
    'tests/test_waveform_verification_tool.py':dict(before='454feeb765ac565dc1a3f789a156274a237130200b233366aaf9f04db5895330',after='54def10125fe07a7b336ee606b944a978e98581573f9a0787ff4e33f83144eea')}
RETAINED_IMPLEMENTATION_BEFORE={
    'tools/verify_waveforms.py':'724590072a7d3d9baf01a5eb658947d89cabc6f7138a8a4a45f0c14815c303c2',
    'tools/waveform_restore.py':'f42081f5cb6e936f97fdab82025708a17ddf19dbf452b594a1d3c0bfd2a85269',
    'tests/waveform_experiments.py':'091efa1efaaf72b0c8991a083c9641b3099201b14923d320cab1df08784de1e7',
    'tests/test_waveform_verification_tool.py':'54def10125fe07a7b336ee606b944a978e98581573f9a0787ff4e33f83144eea'}


def retained_source_transition(old,current,approved):
    from tools.waveform_restore import RETAINED_SOURCE_SHA256
    if inventory_sha256(old)!=RETAINED_SOURCE_SHA256 or inventory_sha256(current)!=approved:
        raise ValueError('Retained source approval mismatch')
    baseline=dict(old)
    for path,pair in RETAINED_DIAGNOSTIC_DELTA.items():
        if baseline.get(path)!=pair['before']: raise ValueError('Retained diagnostic before mismatch')
        baseline[path]=pair['after']
    if inventory_sha256(baseline)!=RETAINED_BASE_SHA256: raise ValueError('Retained baseline mismatch')
    restored=dict(current); delta={}
    for path,before in RETAINED_IMPLEMENTATION_BEFORE.items():
        if baseline.get(path)!=before or path not in current or current[path]==before:
            raise ValueError('Retained implementation mismatch')
        delta[path]=dict(before=before,after=current[path]); restored[path]=before
    if restored!=baseline: raise ValueError('Unapproved retained source delta')
    return dict(id='R92-retained-source-new-target',historical_source_sha256=RETAINED_SOURCE_SHA256,
        baseline_sha256=RETAINED_BASE_SHA256,current_source_sha256=approved,
        diagnostic_delta=RETAINED_DIAGNOSTIC_DELTA,implementation_delta=delta)
FINAL_GATE_BEFORE = {'tests/test_waveform_verification_tool.py': 'f013463bfe5c031b5061b4f92b20022226b264f1019fa512cb3abfce01a2071c',
 'tools/verify_waveforms.py': '79e81164075c05a440a606f01b84e5840d27d9033d775fcbdc72919e12bc8842'}
REVIEWED_SOURCE_CHAIN = (('R81',
  '9b26d226c5799bdbdf0043e986c086e9b2675c03ae5ecbc936e859dfef9d2121',
  '4fee2f295a018c1b2ac23dcb82e08e134119d4c5aff3fdb7c0c107f3620240c6',
  (('tests/test_waveform_end_to_end.py',
    '1b63466fcbe04b8f4849ea691d9df2a7dc73ddc6e056a6ec09be802bb202150c',
    '1f639611582a8b4803aca6aa48d1845718485a0299fe3e75f01831ef540e70e0'),
   ('tests/test_waveform_verification_tool.py',
    '760946027a960f565bd4646cdf000c9b731a7e770b442f8923c9c836a7ef9546',
    '988bbc0a34f2f5697849220fe81c33774dce24f7dd53b4d0065889781056a531'),
   ('tests/waveform_experiments.py',
    '2d7ea4be4d496bfe6cda45d959bfda0ce3bac06e43abbaae61205539ca5b34d9',
    '7ac7cd4d02a511223254e62b783bb670b85545a91a221b43ab51d9af2ad95e5b'),
   ('tools/verify_waveforms.py',
    'c2234e1e982b442da83c80cb2d996a3a69acfdaa488e504d22fde89c3e72dc4f',
    'eb3530ac9b347da7cfad4eb4b48ce3315d8e4426ed714e1702241fb38858155c'))),
 ('R82',
  '4fee2f295a018c1b2ac23dcb82e08e134119d4c5aff3fdb7c0c107f3620240c6',
  'ab55bf226f5c7c31d5ad1988debb4dc37c46ed1143f5d7f03699da4bfe2dab9c',
  (('backend/waveforms/migrate.py',
    '52aac20f908b004a20d1f31c73d05f47f5019be90e35696d728c6e038a7ca5e5',
    '91a101f025f82cf90ab762105083340a5f5176609eb18ef4795171798047f538'),
   ('backend/waveforms/sql/005_portable_structure.sql', None, '4e8cdb6f00099ecbb76eb2fa37765654f3317d3b073ff61f5e01d1ed3d524bda'),
   ('tests/test_waveform_dataset_upgrade.py',
    '2e814e4198c7fe01d433b076f5821bf9f0c3ac65c507152c428fc662d496f048',
    'f08163dce5cfc6d400af5bb12794248a4563f7516b3d1a03603be50f3ca34825'),
   ('tests/test_waveform_end_to_end.py',
    '1f639611582a8b4803aca6aa48d1845718485a0299fe3e75f01831ef540e70e0',
    '6c4905e7f78680e9e267dbeee046806bad4f84f2f3648d7691e5731dd92a4b99'),
   ('tests/test_waveform_schema.py',
    '6688ae605b2c934f31a91323c6baa897a9f59438d667f48ee825bbff923fab0d',
    'd5dcff6967e6abda918374f297fe59535073ccd1ee225447a852d2d10110043e'),
   ('tools/waveform_restore.py',
    '1fe9b99f68034e7f2251920bb4e280958d82d6bb523118cab38c4d6e081a4ec8',
    '9bac5a861ef5687e10c34376d4fc0497d184219f47252a5bed16b2a3e0a5b1f3'))),
 ('R83',
  'ab55bf226f5c7c31d5ad1988debb4dc37c46ed1143f5d7f03699da4bfe2dab9c',
  '53a273a4f1fedb1947ac9c2844e4af711f80b4d19f04afbeefef473b41c781bf',
  (('tests/test_waveform_verification_tool.py',
    '988bbc0a34f2f5697849220fe81c33774dce24f7dd53b4d0065889781056a531',
    'bea6fe035d32949ebf0c70762090a7bd968aafac2e00291bc0a23ded7daa5472'),
   ('tools/verify_waveforms.py',
    'eb3530ac9b347da7cfad4eb4b48ce3315d8e4426ed714e1702241fb38858155c',
    'b9dd91e286ed41e086e9722603ed891cafc5402c1bda350c21947bb670fd69d0'))),
 ('R84prepare',
  '53a273a4f1fedb1947ac9c2844e4af711f80b4d19f04afbeefef473b41c781bf',
  'b762392d13ec682bde026daaefe59e45578bdeab19609f639be716526765a294',
  (('backend/waveforms/dataset_context.py',
    'fc39d62551c3de5066de02dcaa6c223274941a0134bcfceff8b99c73d572b554',
    '56b1cd03bddeabc51c6b10db2293c996d9711b30baef7868ee1efc3f9e219e74'),
   ('backend/waveforms/quality_context.py',
    'e424d03fe6e7b8e9fbbf26ae5c1dd85087e7730e8175b06157456c4ea223be2f',
    '5a2317d96ec8cf4e3c9e985d00e6064d9d1d8b5f2a297af75d223493ecb76f55'),
   ('tests/test_waveform_datasets.py',
    'f80d747128de58ff86737986310a1a2b61b67d5eadec7076c5c90fa6b1f19cc3',
    '9356a829d1f56fe2bc9e0ff22453613c6f67d4e201afaefc614926a852cff021'))),
 ('R85',
  'b762392d13ec682bde026daaefe59e45578bdeab19609f639be716526765a294',
  '29f566172f4394d8289d32b8f1b634afc0862e7991233cd1ed3d6a605bea1848',
  (('backend/waveforms/dataset_context.py',
    '56b1cd03bddeabc51c6b10db2293c996d9711b30baef7868ee1efc3f9e219e74',
    '1190fd6d43159eece324ebfe168ff63665b1a45cff30b31fd96387e9de3ac670'),
   ('tests/test_waveform_datasets.py',
    '9356a829d1f56fe2bc9e0ff22453613c6f67d4e201afaefc614926a852cff021',
    'bc2ee25b12998517acd24e59f12e4ee06510463d95ffa4cec4c3314d92fd9a2d'),
   ('tests/test_waveform_export.py',
    '7ffb590a071621e63b76d79dc9cea09b16432dcaaf9bd30498dfb668b08aaf9c',
    '65b083005ac1c832f3531d6e575cb265a59fa03519daf79fa3300da75c9bf580'))),
 ('R86',
  '29f566172f4394d8289d32b8f1b634afc0862e7991233cd1ed3d6a605bea1848',
  '3eaeafad18c208691cab048ce53fe50652c147e32f741d5f864d2073bdd79dd2',
  (('tests/test_waveform_end_to_end.py',
    '6c4905e7f78680e9e267dbeee046806bad4f84f2f3648d7691e5731dd92a4b99',
    '8a79c1aa6a39f6f3b5236ddb3e643495adea4ee04cc09a034ebb293f357a905c'),
   ('tests/test_waveform_failure_recovery.py',
    'a15c3e70a28f58bae93164c1495c54028345d3f9fa67204d1bb3108e1a0b0ce4',
    'a4f448b5b50d85b7679599ce13a92c459a4d0b23c54a30ae60be460888a261f8'),
   ('tests/test_waveform_verification_tool.py',
    'bea6fe035d32949ebf0c70762090a7bd968aafac2e00291bc0a23ded7daa5472',
    'd96fe0d1236fd907386573ff08813d34fb4633fa117616757df6609f46cb3c69'),
   ('tests/waveform_experiments.py',
    '7ac7cd4d02a511223254e62b783bb670b85545a91a221b43ab51d9af2ad95e5b',
    '8dc08b00a00ea58499bf9309568d7d035b5cff0360f48cddacfec0360f5ddd94'),
   ('tests/waveform_measurements.py',
    'a44649722744e1fdffb39506051d454eaff0181ee7879777f8a63f9f575bebdc',
    'cdd1dbbf220c4d23abe74fe0e3032a3dfb314511673116a347cf171c74782d0e'),
   ('tests/waveform_resource_scenarios.py',
    '35fc5139e58840b4252a3a2f2b6c9eba337417f43eafa41ab5149af053781d57',
    'd96eb35823a0e91f8901701daef2b31e4753bc16f387d60b85644369f515e8a0'),
   ('tests/waveform_stack.py',
    'a3fb7278d3ae0bcb54443027908b94dd114a3bfb2e0f8ff72d86f078439bb076',
    '05c4d9912a45c21d79fb918275100b368ec8915742f7dbc30b33f5dc90d64750'),
   ('tests/waveform_worker.py',
    '63601b368750b9cc244bb398a243a3323e931eecbcce43c237e8af616c8990e3',
    '0c746243e8c8b1545ec28faedcc908bbc67b49beeb068a682ebf6a5b05e9e81a'),
   ('tools/verify_waveforms.py',
    'b9dd91e286ed41e086e9722603ed891cafc5402c1bda350c21947bb670fd69d0',
    '5083b470367ba61f3da1d8bfa02c1574159115d078fdca5ed9c2943420f33fb3'))),
 ('R86fix1',
  '3eaeafad18c208691cab048ce53fe50652c147e32f741d5f864d2073bdd79dd2',
  '818c65c6136c692dcacc01ddf87bca5998985768c9c0e06427903c73e9a4d9e6',
  (('tests/test_waveform_verification_tool.py',
    'd96fe0d1236fd907386573ff08813d34fb4633fa117616757df6609f46cb3c69',
    '72dc6dd64b73aba6401784639d00139686fa418e83c93f85acdbbe68124515b2'),
   ('tests/waveform_resource_scenarios.py',
    'd96eb35823a0e91f8901701daef2b31e4753bc16f387d60b85644369f515e8a0',
    '4739f91f84f65a948fa060737a0709c31549534ffe0a250b3fc7634184ae3d54'))),
 ('R88v2',
  '818c65c6136c692dcacc01ddf87bca5998985768c9c0e06427903c73e9a4d9e6',
  'a2bb37ba2b29585f70ef7a97df80337cc44ee7351e565f95000eb2c4a390bbb3',
  (('backend/waveforms/export.py',
    '843afc2e8e9c781e4e9c4e897e1364b74457c43254b26733c5f9d47183b45c86',
    'bc02fa7ff7338365283b9020a72ee27987fed65d220857529ee110d26072cc3c'),
   ('tests/test_waveform_end_to_end.py',
    '8a79c1aa6a39f6f3b5236ddb3e643495adea4ee04cc09a034ebb293f357a905c',
    'a0f569711f7b6b58b31d8545b65f2b4edbeb73d126ecdd0e3a901596d1808c0e'),
   ('tests/test_waveform_export.py',
    '65b083005ac1c832f3531d6e575cb265a59fa03519daf79fa3300da75c9bf580',
    '12ef674dd6e640efed2864fb54b861ac1c44266730ce33cedb09e4bedb42dba5'),
   ('tests/test_waveform_verification_tool.py',
    '72dc6dd64b73aba6401784639d00139686fa418e83c93f85acdbbe68124515b2',
    '9be0b98ef2e83cfeb171b1e823a829d73d702c66f9e90112bbd29766cac39c93'),
   ('tests/waveform_experiments.py',
    '8dc08b00a00ea58499bf9309568d7d035b5cff0360f48cddacfec0360f5ddd94',
    '4533445d6ca4d517f1900c8a04189425373a54f354ce2c4b0cbb242081c8e713'),
   ('tests/waveform_resource_scenarios.py',
    '4739f91f84f65a948fa060737a0709c31549534ffe0a250b3fc7634184ae3d54',
    'c3d5ce2b02bace2f003a8e74e66b948b3aabc45718696c2b23bd4b4421420833'),
   ('tools/verify_waveforms.py',
    '5083b470367ba61f3da1d8bfa02c1574159115d078fdca5ed9c2943420f33fb3',
    '79e81164075c05a440a606f01b84e5840d27d9033d775fcbdc72919e12bc8842'))),
 ('R88fix1',
  'a2bb37ba2b29585f70ef7a97df80337cc44ee7351e565f95000eb2c4a390bbb3',
  'da297c9efb66848a8772360b9467edb0765815284cbc10b0f3adf9f278cd64cd',
  (('tests/test_waveform_end_to_end.py',
    'a0f569711f7b6b58b31d8545b65f2b4edbeb73d126ecdd0e3a901596d1808c0e',
    'c583131cda5a0090b01a86a87c3f3859f1833cb576c6021c758b6c872618e145'),
   ('tests/test_waveform_verification_tool.py',
    '9be0b98ef2e83cfeb171b1e823a829d73d702c66f9e90112bbd29766cac39c93',
    '88e2c138adc07232287f0f1deef91b44647fd9ec85aad3ab43dad6e56f7efa99'),
   ('tests/waveform_experiments.py',
    '4533445d6ca4d517f1900c8a04189425373a54f354ce2c4b0cbb242081c8e713',
    '12800adecb6acfa85844a42718ee165738f781bbde0899b4e48a42aef8073a4c'),
   ('tests/waveform_resource_scenarios.py',
    'c3d5ce2b02bace2f003a8e74e66b948b3aabc45718696c2b23bd4b4421420833',
    '182497a683020b77553cf491bde70695f20673f03630a4baa108eeef6a3a3b2f'))),
 ('R88fix2',
  'da297c9efb66848a8772360b9467edb0765815284cbc10b0f3adf9f278cd64cd',
  'e5180ca07c74d2ce0f8baa25d00a612ea0f5db471290c37bbf174e1943726516',
  (('tests/test_waveform_verification_tool.py',
    '88e2c138adc07232287f0f1deef91b44647fd9ec85aad3ab43dad6e56f7efa99',
    '5e066116bc4cd743522749b8c03a7fc69df139da1e1aafd383fa0bce15cec584'),
   ('tests/waveform_resource_scenarios.py',
    '182497a683020b77553cf491bde70695f20673f03630a4baa108eeef6a3a3b2f',
    '6b0c650a1ca923f585adf5ecf46cd8cdd9098226f0ed8af5ce4e2c5f0efbdbe3'),
   ('tests/waveform_stack.py',
    '05c4d9912a45c21d79fb918275100b368ec8915742f7dbc30b33f5dc90d64750',
    'f178e60084885e5ac07c5bdfb9530c643c94031388f50fddfde263804994cba2'))),
 ('R89v2',
  'e5180ca07c74d2ce0f8baa25d00a612ea0f5db471290c37bbf174e1943726516',
  '18d070b1d3f0a2c2b33ca3396ba49848baec94b3435a0018ea5522ffb4032f0f',
  (('tests/test_waveform_end_to_end.py',
    'c583131cda5a0090b01a86a87c3f3859f1833cb576c6021c758b6c872618e145',
    '0498af620798f0e56af2c7ce5d4c1819c2e4c4537cdc1ca231eac55dd595b14d'),
   ('tests/test_waveform_verification_tool.py',
    '5e066116bc4cd743522749b8c03a7fc69df139da1e1aafd383fa0bce15cec584',
    '95324a0fd27a1072231a2ace29adb94c6f71ef8129a5f5d93e75e3be1c7e854c'),
   ('tests/waveform_experiments.py',
    '12800adecb6acfa85844a42718ee165738f781bbde0899b4e48a42aef8073a4c',
    '091efa1efaaf72b0c8991a083c9641b3099201b14923d320cab1df08784de1e7'),
   ('tests/waveform_resource_scenarios.py',
    '6b0c650a1ca923f585adf5ecf46cd8cdd9098226f0ed8af5ce4e2c5f0efbdbe3',
    'e06c60d15400a2d9708fb07909cb8b3d570e7eeb5108d8da8e6f2bbc9eb7c370'),
   ('tools/waveform_restore.py',
    '9bac5a861ef5687e10c34376d4fc0497d184219f47252a5bed16b2a3e0a5b1f3',
    'f1df49ffdd0fda7c6802f545ae21253a56d1d11390c63626510e149897a0ba7d'))),
 ('R89fix1',
  '18d070b1d3f0a2c2b33ca3396ba49848baec94b3435a0018ea5522ffb4032f0f',
  'd39a741bb596961e96eed4d4845a0e21b867e5003ec6590c2e8bcbd9e2feaf58',
  (('tests/test_waveform_verification_tool.py',
    '95324a0fd27a1072231a2ace29adb94c6f71ef8129a5f5d93e75e3be1c7e854c',
    'f013463bfe5c031b5061b4f92b20022226b264f1019fa512cb3abfce01a2071c'),
   ('tools/waveform_restore.py',
    'f1df49ffdd0fda7c6802f545ae21253a56d1d11390c63626510e149897a0ba7d',
    'f42081f5cb6e936f97fdab82025708a17ddf19dbf452b594a1d3c0bfd2a85269'))),
 ('R90docs',
  'd39a741bb596961e96eed4d4845a0e21b867e5003ec6590c2e8bcbd9e2feaf58',
  'eb073639f50b045c6bfbcff285378de26aa4bb1ff1254ddde5222c38e314ba8f',
  (('docs/同步波形部署与回退.md',
    '3e88727d2f9cf418b2603fba58c52867e2ff06a14fe6316878eb76188f92b4ea',
    'c672d3eb5af690944629d98df17a31a0a6648be86bb32dbb33b1b798dd4e3121'),
   ('docs/年度工作任务逐项响应.md',
    'acf9d16a897436442c81ca11edc64e00906af0cb23fa5bd4443c52f7f7cee9fb',
    '073f07cc50eec25fd5f27358ffff8a3946afc4f8a81c05b782bf13c837d2cb75'))))

def recovery_product_exception(delta):
    """Exact v4→v5 recovery bridge, called only in three-segment context."""
    extra={p:v for p,v in delta.items() if p not in RESOURCE_CONTINUATION_PATHS}
    if not extra: return None
    if extra!=R82_RECOVERY_DELTA: raise ValueError('Unapproved recovery source delta')
    return dict(id='R82-exact-portable-recovery',scope='recovery_and_regression',
        historical_schema_versions=[4,4],current_schema_version=5,files={p:dict(v) for p,v in extra.items()})


def continuation_product_exception(delta):
    """Only the reviewed R78 exact pair, never a general product allowlist."""
    extra={p:v for p,v in delta.items() if p not in CONTINUATION_PATHS}
    if not extra: return None
    if extra!=R78_PRODUCT_DELTA: raise ValueError('Unapproved source delta')
    return dict(id='R78-exact-calibration-bounds',scope='fresh_suffix_and_regression',files={p:dict(v) for p,v in extra.items()})


def _number(value,minimum=0):
    return type(value) in (int,float) and math.isfinite(value) and value>=minimum


def reviewed_source_transition(current,approved,*,measured=None):
    """Reverse the final two-file gate and fixed reviewed chain; no mutable inputs."""
    import re
    if inventory_sha256(current)!=approved: raise ValueError('Current source approval mismatch')
    if set(FINAL_GATE_BEFORE)!={'tools/verify_waveforms.py','tests/test_waveform_verification_tool.py'}:
        raise ValueError('Invalid final gate paths')
    restored=dict(current); gate={}
    for path,before in FINAL_GATE_BEFORE.items():
        if path not in current or current[path]==before: raise ValueError('Incomplete final gate delta')
        gate[path]=dict(before=before,after=current[path]); restored[path]=before
    if not REVIEWED_SOURCE_CHAIN: raise ValueError('Missing reviewed source chain')
    names=[]; checkpoints=[]
    for edge in reversed(REVIEWED_SOURCE_CHAIN):
        if type(edge) is not tuple or len(edge)!=4: raise ValueError('Invalid reviewed edge')
        name,before,after,changes=edge
        if not isinstance(name,str) or not name or name in names or not changes or type(changes) is not tuple:
            raise ValueError('Duplicate/malformed reviewed edge')
        names.append(name)
        if inventory_sha256(restored)!=after: raise ValueError('Reviewed checkpoint mismatch')
        seen=set()
        for change in changes:
            if type(change) is not tuple or len(change)!=3: raise ValueError('Invalid reviewed change')
            path,old,new=change
            if (not isinstance(path,str) or not path or path in seen or old==new or
                any(v is not None and (not isinstance(v,str) or not re.fullmatch('[0-9a-f]{64}',v)) for v in (old,new))):
                raise ValueError('Duplicate/malformed reviewed path')
            seen.add(path)
            if restored.get(path)!=new: raise ValueError('Reviewed after hash mismatch')
            if old is None: restored.pop(path)
            else: restored[path]=old
        if inventory_sha256(restored)!=before: raise ValueError('Reviewed before hash mismatch')
        checkpoints.append(dict(id=name,before=before,after=after))
    if inventory_sha256(restored)!=RUN3_SOURCE_SHA256 or measured is not None and restored!=measured:
        raise ValueError('Reviewed chain does not end at original run3')
    return dict(measured=restored,source_transition=dict(id='R90-exact-reviewed-chain',scope='recovery_and_regression',
        checkpoints=list(reversed(checkpoints)),gate_delta=gate,approved_source_sha256=approved))


def resource_lineage(prior,middle,current,approved):
    """Shared preflight/final gate: old→middle and middle→approved recovery."""
    import re
    if type(prior) is not dict or type(middle) is not dict: raise ValueError('Invalid resource segments')
    old=prior['files_sha256']; measured=middle['files_sha256']
    for files in (old,measured,current): inventory_sha256(files)
    if set(old)!=set(measured): raise ValueError('Historical source keys changed')
    delta=lambda a,b:{p:dict(before=a.get(p),after=b.get(p)) for p in sorted(set(a)|set(b)) if a.get(p)!=b.get(p)}
    first=delta(old,measured); second=delta(measured,current)
    transition=None
    try: bridge=recovery_product_exception(second)
    except ValueError:
        transition=reviewed_source_transition(current,approved,measured=measured)['source_transition']
        bridge=recovery_product_exception(R82_RECOVERY_DELTA)
    addition={'backend/waveforms/sql/005_portable_structure.sql'} if bridge else set()
    if (set(current)!=set(measured)|addition or
        middle.get('recovery_exception')!=bridge or
        middle.get('source_transition')!=transition or
        (transition is not None and (prior['run_id']!=RUN1_ID or prior['report_sha256']!=RUN1_REPORT_SHA256 or
            middle['run_id']!=RUN3_ID or middle['report_sha256']!=RUN3_REPORT_SHA256)) or
        (bridge and (prior.get('schema_version')!=4 or middle.get('schema_version')!=4)) or
        inventory_sha256(current)!=approved or
        prior['source_delta']!=first or prior.get('product_exception')!=continuation_product_exception(first) or
        middle['source_delta']!=second or prior['source_sha256']!=inventory_sha256(old) or
        middle['source_sha256']!=inventory_sha256(measured) or prior['validated'] is not True or middle['validated'] is not True or
        prior['kind']!='reused_protocol_soak' or middle['kind']!='reused_maximum_xy_audit' or
        prior['run_id']==middle['run_id'] or middle['ancestor_run_id']!=prior['run_id'] or
        middle['ancestor_report_sha256']!=prior['report_sha256'] or
        prior['report_ref']!='prior-run1/report.json' or middle['report_ref']!='prior-run3/report.json' or
        middle['ancestor_report_ref']!='prior-run3/prior-run1/report.json' or
        any(not isinstance(s[k],str) or not re.fullmatch(pattern,s[k]) for s in (prior,middle)
            for k,pattern in (('run_id','[0-9a-f]{32}'),('report_sha256','[0-9a-f]{64}')))):
        raise ValueError('Invalid resource lineage')
    return bridge


def three_segment_assertions(report):
    prior,middle,suffix=report['segments']; source=report['source']
    if type(suffix) is not dict: raise ValueError('Invalid recovery segment')
    bridge=resource_lineage(prior,middle,source['files_sha256'],suffix['approved_source_sha256'])
    if (source['scope']!='recovery_and_regression' or suffix['kind']!='fresh_recovery_and_regression' or
        suffix.get('source_transition')!=middle.get('source_transition') or
        (bridge and suffix.get('schema_version')!=5) or
        suffix['source_sha256']!=suffix['approved_source_sha256'] or suffix['run_id']!=report['run_id'] or
        report['run_id'] in (prior['run_id'],middle['run_id'])): raise ValueError('Invalid recovery scope')
    artifacts={a['relative_path']:a['sha256'] for a in report['artifacts']}
    for ref,digest in ((prior['report_ref'],prior['report_sha256']),(middle['report_ref'],middle['report_sha256']),
        (middle['ancestor_report_ref'],prior['report_sha256'])):
        if artifacts.get(ref)!=digest: raise ValueError('Unbound ancestor report')
    cases=report['scenarios']
    if len(cases)!=14 or {s['id'] for s in cases}!=SCENARIOS: raise ValueError('Incomplete segmented coverage')
    refs_by_case={'online_then_offline':['ingress-GW.json'],'offline_then_online':['ingress-A.json'],
        'receipt_chain':['ingress-GW.json','ingress-A.json'],'transport_judgments':['transport.json'],
        'gateway_isolation':['fault-8.json'],'lease_recovery':['fault-2.json','fault-9.json','fault-10.json'],
        'xy':['xy.json'],'audit_volume':['audit.json'],'dataset_1000':['dataset-1.json','dataset-100.json','dataset-1000.json']}
    for case in cases:
        segment,prefix=(prior,'prior-run1/') if case['id'] in PROTOCOL_SCENARIOS else (middle,'prior-run3/') if case['id'] in RESOURCE_SCENARIOS else (suffix,'')
        refs=case['artifact_refs']
        if case['segment_run_id']!=segment['run_id'] or not isinstance(refs,list) or not refs:
            raise ValueError('Scenario segment identity')
        if refs!=[prefix+name for name in refs_by_case.get(case['id'],['maximum.json'])]:
            raise ValueError('Scenario phase coverage')
        for ref in refs:
            if not isinstance(ref,str) or ref not in artifacts or (prefix and not ref.startswith(prefix)) or (not prefix and ref.startswith('prior-')):
                raise ValueError('Scenario segment artifact')


def _retained_context(report):
    """Only the fixed R90 supplement may use historical lease/recovery identity."""
    from tools.waveform_restore import RETAINED_RUN_ID,RETAINED_REPORT_SHA256,RETAINED_SOURCE_SHA256
    if report.get('evidence_mode')!='four_segment': return report
    prior,middle,retained,suffix=report['segments']
    if (type(retained) is not dict or retained['kind']!='reused_source_preparation_and_lease' or
        retained['run_id']!=RETAINED_RUN_ID or retained['report_sha256']!=RETAINED_REPORT_SHA256 or
        retained['report_ref']!='prior-r90/report.json' or retained['source_sha256']!=RETAINED_SOURCE_SHA256 or
        inventory_sha256(retained['files_sha256'])!=RETAINED_SOURCE_SHA256 or retained['historical_status']!='partial'):
        raise ValueError('Invalid retained supplement identity')
    return dict(run_id=RETAINED_RUN_ID,source=dict(files_sha256=retained['files_sha256']),
        lease_measurement=report['lease_measurement'],recovery_downloads=report['recovery_downloads'],
        artifacts=[dict(a,relative_path=a['relative_path'][10:]) for a in report['artifacts'] if a['relative_path'].startswith('prior-r90/')])


def four_segment_assertions(report):
    from tools.waveform_restore import RETAINED_RUN_ID,RETAINED_REPORT_SHA256,RETAINED_SOURCE_SHA256,RETAINED_ARCHIVE_SHA256
    prior,middle,retained,suffix=report['segments']; source=report['source']; current=source['files_sha256']
    if any(type(s) is not dict for s in (prior,middle,retained,suffix)): raise ValueError('Invalid segment container')
    _retained_context(report)
    resource_lineage(prior,middle,retained['files_sha256'],RETAINED_SOURCE_SHA256)
    transition=retained_source_transition(retained['files_sha256'],current,suffix['approved_source_sha256'])
    if (source['scope']!='retained_target_and_regression' or suffix['kind']!='fresh_target_and_regression' or
        suffix['source_transition']!=transition or suffix['source_sha256']!=inventory_sha256(current) or
        suffix['run_id']!=report['run_id'] or len({s['run_id'] for s in (prior,middle,retained,suffix)})!=4 or
        retained['validated'] is not True or retained['excluded_target']!='oilwell_waveforms_restore_'+RETAINED_RUN_ID or
        retained['excluded_reason']!='safety.host_reserve'): raise ValueError('Invalid fresh target scope')
    artifacts={a['relative_path']:a['sha256'] for a in report['artifacts']}
    for segment in (prior,middle,retained):
        if artifacts.get(segment['report_ref'])!=segment['report_sha256']: raise ValueError('Unbound historical report')
    refs={'online_then_offline':['ingress-GW.json'],'offline_then_online':['ingress-A.json'],
        'receipt_chain':['ingress-GW.json','ingress-A.json'],'transport_judgments':['transport.json'],
        'gateway_isolation':['fault-8.json'],'lease_recovery':['fault-2.json','fault-9.json','fault-10.json'],
        'xy':['xy.json'],'audit_volume':['audit.json'],'dataset_1000':['dataset-1.json','dataset-100.json','dataset-1000.json']}
    cases=report['scenarios']
    if len(cases)!=14 or {c['id'] for c in cases}!=SCENARIOS: raise ValueError('Incomplete historical coverage')
    for case in cases:
        segment,folder=(prior,'prior-run1/') if case['id'] in PROTOCOL_SCENARIOS else (middle,'prior-run3/') if case['id'] in RESOURCE_SCENARIOS else (retained,'prior-r90/')
        if (case['segment_run_id']!=segment['run_id'] or case['artifact_refs']!=[folder+n for n in refs.get(case['id'],['maximum.json'])] or
            any(n not in artifacts for n in case['artifact_refs'])): raise ValueError('Invalid historical scenario assignment')
    cleanup=report['cleanup']['source_guard']; recovery=report['restore']; supervised=recovery['export_supervision']; delta=recovery['deltas']
    if (cleanup['complete'] is not True or cleanup['source_unchanged'] is not True or cleanup['errors'] or
        cleanup['first_error'] is not None or cleanup['unlocked']!=[499001213,499001211] or
        not cleanup['closed'] or any(v is not True for v in cleanup['closed']) or
        recovery['target']!='oilwell_waveforms_restore_'+report['run_id'] or recovery['archive_sha256']!=RETAINED_ARCHIVE_SHA256 or
        supervised['status']!='passed' or supervised['owned_cleanup']['complete'] is not True or
        supervised['result']['run_id']!=report['run_id'] or supervised['result']['source_sha256']!=inventory_sha256(current) or
        delta['unchanged_other_tables'] is not True or delta['business_audits']!=1107 or not _number(delta['raw_audits'],1103)):
        raise ValueError('Fresh target or cleanup incomplete')


def qualify(report: dict) -> tuple[str,...]:
    """Pure gate. Dictionary unit fixtures do not authenticate real execution."""
    reasons=[]
    if type(report) is not dict: return ('report.invalid',)
    def section(name):
        value=report.get(name)
        if type(value) is not dict:
            reasons.append(name+'.missing'); return {}
        return value
    def flags(name,keys):
        value=section(name)
        if any(value.get(key) is not True for key in keys): reasons.append(name+'.incomplete')
        return value
    if report.get('schema')!='waveform.verification/1' or report.get('status')!='completed' or report.get('evidence_kind')!='actual':
        reasons.append('report.incomplete')
    import re
    if not isinstance(report.get('run_id'),str) or not re.fullmatch('[0-9a-f]{32}',report['run_id']): reasons.append('identity.run_id')
    source=flags('source',('unchanged',))
    if not valid_lease_measurement(report): reasons.append('lease.invalid')
    if not source.get('files_sha256'): reasons.append('source.inventory')
    if report.get('evidence_mode')=='two_segment':
        segments=report.get('segments',[])
        try:
            prior,suffix=segments
            if not isinstance(prior,dict) or not isinstance(suffix,dict): raise ValueError()
            old=prior['files_sha256']; current=source['files_sha256']
            inventory_sha256(old); inventory_sha256(current)
            delta={p:dict(before=old.get(p),after=current.get(p)) for p in sorted(set(old)|set(current)) if old.get(p)!=current.get(p)}
            exception=continuation_product_exception(delta)
            if (prior['kind']!='reused_protocol_soak' or suffix['kind']!='fresh_suffix_and_regression' or
                prior['run_id']==report['run_id'] or suffix['run_id']!=report['run_id'] or
                prior['validated'] is not True or source.get('scope')!='suffix_and_regression' or
                not re.fullmatch('[0-9a-f]{64}',prior['report_sha256']) or
                prior['source_sha256']!=inventory_sha256(prior['files_sha256']) or
                suffix['approved_source_sha256']!=inventory_sha256(source['files_sha256']) or
                suffix['source_sha256']!=suffix['approved_source_sha256'] or
                set(old)!=set(current) or prior['source_delta']!=delta or prior.get('product_exception')!=exception or
                not any(a.get('relative_path')==prior['report_ref'] and a.get('sha256')==prior['report_sha256'] for a in report.get('artifacts',[]) if isinstance(a,dict))): raise ValueError()
        except (KeyError,TypeError,ValueError): reasons.append('segments.invalid')
    elif report.get('evidence_mode')=='three_segment':
        try: three_segment_assertions(report)
        except (KeyError,TypeError,ValueError): reasons.append('segments.invalid')
    elif report.get('evidence_mode')=='four_segment':
        try: four_segment_assertions(report)
        except (KeyError,TypeError,ValueError,AttributeError): reasons.append('segments.invalid')
    elif report.get('segments') is not None: reasons.append('segments.invalid')
    flags('identity',('verified',)); flags('environment',('ready',))
    duration=section('duration')
    elapsed=duration.get('monotonic_elapsed_seconds')
    if not _number(elapsed,1800): reasons.append('duration.too_short')
    progress=duration.get('progress_seconds')
    if (not isinstance(progress,list) or len(progress)<2 or
        any(not _number(v) for v in progress) or
        any(b<=a or b-a>90 for a,b in zip(progress,progress[1:])) or
        progress[0]>1 or not _number(elapsed) or progress[-1]<elapsed-90 or progress[-1]>elapsed+1 or
        not _number(duration.get('messages'),1) or not _number(duration.get('bytes'),1)):
        reasons.append('duration.no_continuous_work')
    flags('ingress',('online','offline','same_bytes','distinct_associations'))
    cases=report.get('scenarios')
    if not isinstance(cases,list): cases=[]
    names=[s.get('id') for s in cases if isinstance(s,dict) and isinstance(s.get('id'),str)]
    if (set(names)!=SCENARIOS or len(names)!=len(SCENARIOS) or len(cases)!=len(names) or
        any(not isinstance(s,dict) or s.get('status')!='passed' or not _number(s.get('assertions'),1) or not s.get('artifact_refs') for s in cases)):
        reasons.append('scenarios.incomplete')
    cycles=report.get('fault_cycles')
    if not isinstance(cycles,list): cycles=[]
    valid=[c for c in cycles if isinstance(c,dict) and c.get('observed') is True and c.get('recovered') is True
           and c.get('kind') in ('process_restart','link_loss','transaction_fault') and c.get('instance_id')
           and _number(c.get('duration_seconds')) and type(c.get('index')) is int]
    if len(valid)<10 or len({c['index'] for c in valid})!=len(valid): reasons.append('faults.insufficient')
    receipts=flags('receipts',('actual_protocol','wrong_rejected','unknown_rejected','duplicate_safe','lost_recovered'))
    if any(not _number(receipts.get(k),1) for k in ('published','parsed','matched')): reasons.append('receipts.no_actual_chain')
    integrity=flags('integrity',('exact_bytes','digests_match'))
    if type(integrity.get('duplicates')) is not int or integrity['duplicates']!=0: reasons.append('integrity.duplicates')
    backlog=section('backlog')
    if type(backlog.get('final')) is not int or backlog['final']!=0: reasons.append('backlog.remaining')
    flags('quota',('isolated',))
    resources=flags('resources',('measured',))
    if resources.get('aborts')!=[]: reasons.append('resources.aborted')
    flags('restore',RESTORE_CHECKS); flags('protection',('unchanged',))
    if not valid_recovery_measurement(report): reasons.append('recovery.invalid')
    cleanup=flags('cleanup',('complete',))
    if cleanup.get('residue')!=[]: reasons.append('cleanup.residue')
    tests=section('tests')
    if report.get('evidence_mode') in ('two_segment','three_segment','four_segment'):
        try:
            if tests.get('run_id')!=report.get('run_id') or tests.get('source_sha256')!=inventory_sha256(source['files_sha256']):
                raise ValueError()
        except (KeyError,TypeError,ValueError): reasons.append('tests.segment_identity')
    if tests.get('scope')!='whole_project' or tests.get('configured') is not True: reasons.append('tests.unconfigured')
    for key in ('failures','errors','skipped','expected_failures','unexpected_successes','exit_code'):
        if type(tests.get(key)) is not int or tests[key]!=0: reasons.append('tests.'+key)
    discovered,started,terminal=tests.get('discovered_ids'),tests.get('started_ids'),tests.get('terminal_results')
    valid_ids=lambda items:isinstance(items,list) and bool(items) and all(isinstance(i,str) and i for i in items)
    if (not valid_ids(discovered) or not valid_ids(started) or not isinstance(terminal,list) or
        type(tests.get('testsRun')) is not int or tests['testsRun']!=len(discovered) or
        Counter(discovered)!=Counter(started) or len(set(discovered))!=len(discovered) or
        len(terminal)!=len(discovered) or any(not isinstance(t,dict) or not isinstance(t.get('id'),str) or t.get('outcome')!='passed' for t in terminal) or
        Counter(t.get('id') for t in terminal if isinstance(t,dict))!=Counter(discovered)):
        reasons.append('tests.partial')
    if tests.get('loader_errors')!=[] or not tests.get('modules'): reasons.append('tests.discovery')
    if not valid_ids(discovered) or any(not any(part in i for i in discovered) for part in ('test_waveform_','test_registry_','test_ingest_')):
        reasons.append('tests.coverage')
    artifacts=report.get('artifacts')
    if not isinstance(artifacts,list) or not artifacts: reasons.append('artifacts.missing')
    else:
        paths=set()
        for item in artifacts:
            if (not isinstance(item,dict) or not isinstance(item.get('relative_path'),str) or
                not isinstance(item.get('sha256'),str) or not re.fullmatch('[0-9a-f]{64}',item['sha256']) or
                type(item.get('size')) is not int or item['size']<0):
                reasons.append('artifacts.invalid'); continue
            paths.add(item['relative_path'])
        if len(paths)!=len(artifacts): reasons.append('artifacts.invalid')
        for case in cases:
            refs=case.get('artifact_refs') if isinstance(case,dict) else None
            if not isinstance(refs,list) or not refs or any(not isinstance(r,str) or r not in paths for r in refs):
                reasons.append('artifacts.unbound_scenario')
    if not report.get('limits'): reasons.append('limits.missing')
    return tuple(dict.fromkeys(reasons))


def digest_file(path):
    with Path(path).open('rb') as handle:
        digest=sha256()
        for block in iter(lambda:handle.read(1024*1024),b''): digest.update(block)
    return digest.hexdigest()


def valid_lease_measurement(report):
    """Fresh dedicated supplement; the historical lease scenario is insufficient."""
    assertions=('one_claim','owned_64','full_unowned_refused','expired_owned_64','reclaimed_owned_only',
        'token_version_changed','stale_renew_refused','stale_run_refused','stale_completion_unchanged',
        'replacement_completed','rollback_refused','rollback_cleared_owned','two_workers_renewed',
        'two_workers_completed','workers_clean','query_plans')
    try:
        report=_retained_context(report)
        value=report['lease_measurement']; dual=value['dual_workers']; plans=value['plans']; cleanup=value['cleanup']
        if (value['schema']!='waveform.lease-measurement/1' or value['status']!='passed' or
            value['run_id']!=report['run_id'] or value['source_sha256']!=inventory_sha256(report['source']['files_sha256']) or
            any(value['assertions'].get(k) is not True for k in assertions) or
            cleanup['complete'] is not True or not cleanup['processes'] or
            any(type(p['exit_code']) is not int or (p['component']!='broker' and p['exit_code']!=0) for p in cleanup['processes']) or
            dual['peak_connections']!=4 or dual['configured_connections']!=4 or dual['completed']!=2 or
            not value['timings'] or any(not _number(t['elapsed_seconds']) or t['elapsed_seconds']>120 for t in value['timings']) or
            len(plans)!=8 or Counter((p['cardinality'],p['state']) for p in plans)!=Counter({(1,'available'):2,(64,'available'):2,(66,'full-live'):2,(66,'expired-still-owned'):2}) or
            any(not p['query'].startswith('SELECT ') or 'FOR UPDATE' in p['query'] or not isinstance(p['params'],list) or
                not _number(p['client_seconds']) or not _number(p['plan']['Execution Time']) or
                not _number(p['plan']['Plan']['Actual Rows']) or not _number(p['plan']['Plan']['Shared Hit Blocks']) for p in plans) or
            len([a for a in report['artifacts'] if a['relative_path']=='lease-measurement.json'])!=1): return False
        if (dual['sampler_joined'] is not True or dual['owned_cleanup']['complete'] is not True or
            dual['stopped']['remaining_threads']!=[] or dual['stopped']['connections']!=0 or
            dual['metadata']['run_id']!=report['run_id'] or dual['metadata']['source_sha256']!=value['source_sha256'] or
            dual['metadata']['seconds']!=120 or not dual['continuous_samples']): return False
        from tests.waveform_measurements import safety_check
        for sample in dual['continuous_samples']:
            if (len(sample['processes'])!=2 or any(not _number(p[k],1) for p in sample['processes']
                for k in ('working_set_bytes','peak_working_set_bytes','private_commit_bytes'))): return False
            safety_check(sample['host'],sample['processes'])
        return True
    except (KeyError,TypeError,ValueError,AttributeError,RuntimeError): return False


def verify_lease_artifact(root,report):
    """Authenticate actual bytes before the expensive gate, and again at finalization."""
    try:
        if report.get('evidence_mode')=='four_segment':
            from tools.waveform_restore import validate_retained_copy
            bundle=validate_retained_copy(root,inventory_sha256(report['source']['files_sha256']))
            if report['lease_measurement']!=bundle['report']['lease_measurement']: raise ValueError()
            return verify_lease_artifact(Path(root)/'prior-r90',bundle['report'])
        items=[a for a in report['artifacts'] if a['relative_path']=='lease-measurement.json']
        if len(items)!=1 or verify_artifacts(root,items): raise ValueError()
        value=json.loads((Path(root)/'lease-measurement.json').read_text(encoding='utf-8'))
        if value!=report['lease_measurement'] or not valid_lease_measurement(report): raise ValueError()
        return ()
    except (KeyError,TypeError,ValueError,OSError): return ('lease.artifact_invalid',)


def valid_recovery_measurement(report):
    from tests.waveform_resource_scenarios import validate_recovery_measurement
    try:
        report=_retained_context(report)
        validate_recovery_measurement(report['recovery_downloads'],run_id=report['run_id'],source_sha256=inventory_sha256(report['source']['files_sha256']))
        return True
    except (KeyError,TypeError,ValueError): return False


def verify_recovery_artifact(root,report):
    """Authenticate the handoff AND every complete file before final discovery."""
    try:
        if report.get('evidence_mode')=='four_segment': return verify_retained_artifacts(root,report)
        value=report['recovery_downloads']
        if not valid_recovery_measurement(report): raise ValueError()
        expected={f['relative_path']:dict(relative_path=f['relative_path'],size=f['size'],sha256=f['sha256']) for f in value['downloads']}
        names={'recovery-downloads.json',*expected}
        items=[a for a in report['artifacts'] if a['relative_path'] in names]
        if len(items)!=len(names) or len({a['relative_path'] for a in items})!=len(names) or verify_artifacts(root,items): raise ValueError()
        if any(a!=expected[a['relative_path']] for a in items if a['relative_path'] in expected): raise ValueError()
        if json.loads((Path(root)/'recovery-downloads.json').read_text(encoding='utf-8'))!=value: raise ValueError()
        return ()
    except (KeyError,TypeError,ValueError,OSError): return ('recovery.artifact_invalid',)


def verify_artifacts(root,items):
    root=Path(root).resolve(); errors=[]
    for item in items:
        try:
            relative=Path(item['relative_path'])
            path=(root/relative).resolve()
            if relative.is_absolute() or '..' in relative.parts or not path.is_relative_to(root) or path==root:
                raise ValueError()
            if path.stat().st_size!=item['size'] or digest_file(path)!=item['sha256']: raise ValueError()
        except (KeyError,TypeError,ValueError,OSError): errors.append('artifacts.invalid')
    return tuple(dict.fromkeys(errors))


def source_inventory():
    result=subprocess.run(['git','ls-files','-z','--cached','--others','--exclude-standard'],cwd=ROOT,
                          capture_output=True,check=True,timeout=15)
    return {name:digest_file(ROOT/name) for name in sorted(set(result.stdout.decode('utf-8').split('\0')))
            if name and (ROOT/name).is_file()}


def inventory_sha256(inventory):
    """SHA256 of UTF8 JSON: sorted keys, no whitespace, ensure_ascii=False."""
    import re
    if not isinstance(inventory,dict) or not inventory or any(not isinstance(k,str) or not k or
        not isinstance(v,str) or not re.fullmatch('[0-9a-f]{64}',v) for k,v in inventory.items()):
        raise ValueError('Invalid source inventory')
    return sha256(json.dumps(inventory,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode('utf-8')).hexdigest()


def validate_continuation(path,expected_sha256,approved_source_sha256,current,*,recovery=False):
    """Read-only preflight; no DB, worker, copy or new run directory here."""
    from tools.waveform_restore import validate_run_root
    from tests.waveform_experiments import validate_completed_prefix
    path=Path(path)
    try:
        root=validate_run_root(path.parent,existing=True)
        if path!=root/'report.json' or path.resolve()!=path or digest_file(path)!=expected_sha256:
            raise ValueError('Original report binding mismatch')
        if inventory_sha256(current)!=approved_source_sha256: raise ValueError('Current source approval mismatch')
        report=json.loads(path.read_text(encoding='utf-8')); old=report['source']['files_sha256']
        inventory_sha256(old)
        delta={p:dict(before=old.get(p),after=current.get(p)) for p in sorted(set(old)|set(current)) if old.get(p)!=current.get(p)}
        bridge=None; transition=None
        try:
            if recovery and any(p in delta for p in R82_RECOVERY_DELTA):
                bridge=recovery_product_exception({p:v for p,v in delta.items() if p not in CONTINUATION_PATHS and p not in R78_PRODUCT_DELTA})
            addition={'backend/waveforms/sql/005_portable_structure.sql'} if bridge else set()
            if set(current)!=set(old)|addition: raise ValueError('Unapproved source delta')
            exception=continuation_product_exception({p:v for p,v in delta.items() if not bridge or p not in R82_RECOVERY_DELTA})
        except ValueError:
            if not recovery: raise
            reviewed=reviewed_source_transition(current,approved_source_sha256)
            measured=reviewed['measured']; transition=reviewed['source_transition']
            if expected_sha256!=RUN1_REPORT_SHA256 or report['run_id']!=RUN1_ID or set(old)!=set(measured):
                raise ValueError('Reviewed chain requires original run1')
            historical_delta={p:dict(before=old[p],after=measured[p]) for p in old if old[p]!=measured[p]}
            exception=continuation_product_exception(historical_delta)
            bridge=recovery_product_exception(R82_RECOVERY_DELTA)
        if (report['run_id']!=root.name[-32:] or report['schema']!='waveform.verification/1' or
            report['evidence_kind']!='actual' or report['status']!='partial' or report.get('segments') or
            report.get('tests') or report.get('restore') or report['source']['unchanged'] is not True or
            report['protection']['unchanged'] is not True or report['protection']['before']!=report['protection']['after'] or
            report['cleanup']!=dict(complete=True,residue=[]) or report['identity']['verified'] is not True or
            report['identity']['run_id']!=report['run_id'] or report['identity']['source_database']!='oilwell_waveforms_test'):
            raise ValueError('Original completed prefix prerequisites')
        items=report['artifacts']; names=[a['relative_path'] for a in items]
        if len(set(names))!=len(names) or not items: raise ValueError('Duplicate/missing original artifacts')
        for item in items:
            relative=Path(item['relative_path']); full=root/relative
            if (relative.is_absolute() or '..' in relative.parts or full.resolve()!=full or
                type(item['size']) is not int or item['size']<0): raise ValueError('Unowned original artifact')
        if verify_artifacts(root,items): raise ValueError('Original artifact binding mismatch')
        prefix=validate_completed_prefix(root,report)
        return dict(root=str(root),report_sha256=expected_sha256,report=report,prefix=prefix,
            source_delta=delta,approved_source_sha256=approved_source_sha256,product_exception=exception,recovery_exception=bridge,
            **({'source_transition':transition} if transition is not None else {}))
    except (KeyError,TypeError,IndexError,OSError,json.JSONDecodeError) as error:
        raise ValueError('Invalid continuation evidence') from error


def validate_resources_continuation(path,expected_sha256,original,current):
    """The single known pre-population run3 branch, authenticated before effects."""
    from tools.waveform_restore import validate_run_root
    from tests.waveform_experiments import validate_completed_resources
    path=Path(path)
    try:
        root=validate_run_root(path.parent,existing=True)
        if path!=root/'report.json' or path.resolve()!=path or digest_file(path)!=expected_sha256:
            raise ValueError('Middle report binding mismatch')
        report=json.loads(path.read_text(encoding='utf-8')); measured=report['source']['files_sha256']
        inventory_sha256(measured); inventory_sha256(current)
        prior,suffix=report['segments']
        segment=dict(kind='reused_maximum_xy_audit',run_id=report['run_id'],validated=True,report_sha256=expected_sha256,
            report_ref='prior-run3/report.json',files_sha256=measured,source_sha256=inventory_sha256(measured),
            ancestor_run_id=original['report']['run_id'],ancestor_report_sha256=original['report_sha256'],
            ancestor_report_ref='prior-run3/prior-run1/report.json',
            source_delta={p:dict(before=measured.get(p),after=current.get(p)) for p in sorted(set(measured)|set(current)) if measured.get(p)!=current.get(p)})
        transition=None
        try: bridge=recovery_product_exception(segment['source_delta'])
        except ValueError:
            transition=reviewed_source_transition(current,original['approved_source_sha256'],measured=measured)['source_transition']
            bridge=recovery_product_exception(R82_RECOVERY_DELTA)
        if bridge!=original.get('recovery_exception'): raise ValueError('Recovery context mismatch')
        if transition!=original.get('source_transition'): raise ValueError('Reviewed source context mismatch')
        if transition is not None: segment['source_transition']=transition
        prior_segment=dict(prior)
        if bridge:
            prior_segment['schema_version']=4
            segment.update(schema_version=4,recovery_exception=bridge)
        resource_lineage(prior_segment,segment,current,original['approved_source_sha256'])
        if (prior['files_sha256']!=original['report']['source']['files_sha256'] or
            report['run_id']!=root.name[-32:] or report['schema']!='waveform.verification/1' or
            report['evidence_kind']!='actual' or report['status']!='partial' or report['evidence_mode']!='two_segment' or
            report['source']['scope']!='suffix_and_regression' or report['source']['unchanged'] is not True or
            report['protection']['unchanged'] is not True or report['protection']['before']!=report['protection']['after'] or
            report['cleanup']!=dict(complete=True,residue=[]) or report.get('tests') or report.get('restore') or
            report['identity']!=dict(verified=True,source_database='oilwell_waveforms_test',run_id=report['run_id']) or
            suffix!=dict(kind='fresh_suffix_and_regression',run_id=report['run_id'],source_sha256=inventory_sha256(measured),approved_source_sha256=inventory_sha256(measured)) or
            any(report[k]!=v for k,v in original['prefix'].items())):
            raise ValueError('Middle prerequisites or original prefix changed')
        frames=[(str(Path(f['file']).relative_to(ROOT)).replace('\\','/'),f['line'],f['function']) for f in report['error_frames']]
        if report['error_type']!='WaveformError' or frames!=[
            ('tools/verify_waveforms.py',339,'main'),('tests/waveform_experiments.py',368,'run_experiments'),
            ('tests/waveform_experiments.py',245,'prepare_recovery'),('backend/waveforms/export.py',111,'open'),
            ('backend/waveforms/dataset_contracts.py',23,'fail')]: raise ValueError('Not the known pre-population denial branch')
        items=report['artifacts']; names=[a['relative_path'] for a in items]
        if not items or len(set(names))!=len(names): raise ValueError('Missing or duplicate middle artifacts')
        for item in items:
            relative=Path(item['relative_path']); full=root/relative
            if (relative.is_absolute() or '..' in relative.parts or full.resolve()!=full or
                type(item['size']) is not int or item['size']<0): raise ValueError('Unsafe middle artifact')
            if not item['relative_path'].startswith('prior-run1/') and (relative.parts[0].startswith(('dataset-','source-','restored-','restore-','population','regression','full-regression'))):
                raise ValueError('Unexpected later-phase artifact')
        if verify_artifacts(root,items): raise ValueError('Middle artifacts changed')
        by_name={a['relative_path']:a for a in items}
        copied=by_name['prior-run1/report.json']
        if copied['sha256']!=original['report_sha256'] or json.loads((root/'prior-run1/report.json').read_text(encoding='utf-8'))!=original['report']:
            raise ValueError('Forged middle ancestor')
        for item in original['report']['artifacts']:
            if by_name['prior-run1/'+item['relative_path']]!=dict(item,relative_path='prior-run1/'+item['relative_path']):
                raise ValueError('Ancestor artifact binding changed')
        phases=[json.loads(line) for line in (root/'phases.jsonl').read_text(encoding='utf-8').splitlines()]
        if 'phases.jsonl' not in by_name or [p['phase'] for p in phases]!=['validated-prior-protocol-soak','maximum-resources','xy-audit','populated-recovery']:
            raise ValueError('Incomplete middle phase history')
        prefix=validate_completed_resources(root,report)
        return dict(root=str(root),report_sha256=expected_sha256,report=report,prefix=prefix,segment=segment,prior_segment=prior_segment)
    except (KeyError,TypeError,IndexError,OSError,json.JSONDecodeError) as error:
        raise ValueError('Invalid resources continuation evidence') from error


def validate_retained_continuation(path,expected_sha256,approved,current,*,original_path,original_sha256,resources_path,resources_sha256):
    """Fixed three external reports/current source; authentication before effects."""
    from tools.waveform_restore import validate_run_root,retained_bundle,RETAINED_REPORT_SHA256,RETAINED_SOURCE_SHA256
    if (expected_sha256!=RETAINED_REPORT_SHA256 or original_sha256!=RUN1_REPORT_SHA256 or
        resources_sha256!=RUN3_REPORT_SHA256 or inventory_sha256(current)!=approved): raise ValueError('External retained pins differ')
    path=Path(path); root=validate_run_root(path.parent,existing=True)
    if path!=root/'report.json' or path.resolve()!=path or digest_file(path)!=expected_sha256: raise ValueError('Retained report path or digest differs')
    old=json.loads(path.read_text(encoding='utf-8'))
    transition=retained_source_transition(old['source']['files_sha256'],current,approved)
    # Existing validators get their actual historical R90 lineage, not the new source.
    original=validate_continuation(original_path,original_sha256,RETAINED_SOURCE_SHA256,old['source']['files_sha256'],recovery=True)
    resources=validate_resources_continuation(resources_path,resources_sha256,original,old['source']['files_sha256'])
    bundle=retained_bundle(root,authenticate_all=True)
    three_segment_assertions(old)
    if (old['segments'][:2]!=[resources['prior_segment'],resources['segment']] or
        not valid_lease_measurement(old) or not valid_recovery_measurement(old) or verify_lease_artifact(root,old) or verify_recovery_artifact(root,old)):
        raise ValueError('Retained historical supplement incomplete')
    return dict(root=str(root),bundle=bundle,original=original,resources=resources,transition=transition,
        approved_source_sha256=approved,report_sha256=expected_sha256)


def copy_retained_continuation(binding,root,run_id):
    from tools.waveform_restore import RETAINED_RUN_ID,RETAINED_REPORT_SHA256,RETAINED_SOURCE_SHA256,validate_retained_copy
    import shutil
    root=Path(root); source=Path(binding['root']); target=root/'prior-r90'; target.mkdir(exist_ok=False)
    if run_id in (RUN1_ID,RUN3_ID,RETAINED_RUN_ID) or run_id!=root.name[-32:]: raise ValueError('Reused suffix identity')
    items=[*binding['bundle']['closure'],dict(relative_path='report.json',size=(source/'report.json').stat().st_size,sha256=RETAINED_REPORT_SHA256)]
    for item in items:
        destination=target/item['relative_path']; destination.parent.mkdir(parents=True,exist_ok=True)
        with (source/item['relative_path']).open('rb') as incoming,destination.open('xb') as output: shutil.copyfileobj(incoming,output,1024*1024)
    mapping=dict(schema='waveform.retained-source/1',run_id=run_id,source_sha256=binding['approved_source_sha256'],
        origin_report_sha256=RETAINED_REPORT_SHA256,origin_run_id=RETAINED_RUN_ID,origin_source_sha256=RETAINED_SOURCE_SHA256,
        files=[dict(item,copied_relative_path='prior-r90/'+item['relative_path']) for item in binding['bundle']['closure']])
    with (root/'retained-source-binding.json').open('x',encoding='utf-8') as output: json.dump(mapping,output,ensure_ascii=False,indent=2)
    validate_retained_copy(root,binding['approved_source_sha256'])


def verify_retained_artifacts(root,report):
    from tools.waveform_restore import validate_retained_copy,target_export_request,_verify_target_exports
    try:
        four_segment_assertions(report); root=Path(root)
        bundle=validate_retained_copy(root,inventory_sha256(report['source']['files_sha256']))
        for directory,digest in (('prior-run1',RUN1_REPORT_SHA256),('prior-run3',RUN3_REPORT_SHA256)):
            path=root/directory/'report.json'
            if path.resolve()!=path or digest_file(path)!=digest: raise ValueError()
            old=json.loads(path.read_text(encoding='utf-8'))
            if verify_artifacts(root/directory,old['artifacts']): raise ValueError()
        if (report['recovery_downloads']!=bundle['report']['recovery_downloads'] or
            report['lease_measurement']!=bundle['report']['lease_measurement'] or
            json.loads((root/'restore-result.json').read_text(encoding='utf-8'))!=report['restore'] or
            json.loads((root/'retained-source-cleanup.json').read_text(encoding='utf-8'))!=report['cleanup']['source_guard']): raise ValueError()
        request=target_export_request(root,report['run_id'],inventory_sha256(report['source']['files_sha256']),bundle['recovery']['records'],bundle['references'])
        _verify_target_exports(request,report['restore']['export_supervision']['result'])
        return ()
    except (KeyError,TypeError,ValueError,AttributeError,OSError): return ('retained.artifact_invalid',)


def copy_continuation(binding,root):
    return _copy_continuation(binding,root,'prior-run1')


def copy_resources_continuation(binding,root):
    return _copy_continuation(binding,root,'prior-run3')


def _copy_continuation(binding,root,directory):
    """Copy only already-validated ordinary evidence into new exclusive root."""
    import shutil
    source=Path(binding['root']); target=Path(root)/directory; target.mkdir(exist_ok=False)
    items=[*binding['report']['artifacts'],dict(relative_path='report.json',size=(source/'report.json').stat().st_size,sha256=binding['report_sha256'])]
    for item in items:
        relative=Path(item['relative_path']); destination=target/relative; destination.parent.mkdir(parents=True,exist_ok=True)
        with (source/relative).open('rb') as incoming,destination.open('xb') as output: shutil.copyfileobj(incoming,output,1024*1024)
    if verify_artifacts(target,items): raise ValueError('Copied continuation evidence changed')
    return binding['prefix']


def _cases(suite):
    for case in suite:
        if isinstance(case,unittest.TestSuite): yield from _cases(case)
        else: yield case


class Results(unittest.TextTestResult):
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs); self.started_ids=[]; self.terminal=[]
    def startTest(self,test): self.started_ids.append(test.id()); super().startTest(test)
    def _record(self,test,outcome): self.terminal.append(dict(id=test.id(),outcome=outcome))
    def addSuccess(self,test): super().addSuccess(test); self._record(test,'passed')
    def addFailure(self,test,error): super().addFailure(test,error); self._record(test,'failed')
    def addError(self,test,error): super().addError(test,error); self._record(test,'error')
    def addSkip(self,test,reason): super().addSkip(test,reason); self._record(test,'skipped')
    def addExpectedFailure(self,test,error): super().addExpectedFailure(test,error); self._record(test,'expected_failure')
    def addUnexpectedSuccess(self,test): super().addUnexpectedSuccess(test); self._record(test,'unexpected_success')


def run_suite(suite,stream,*,modules,configured,loader_errors=()):
    ids=[case.id() for case in _cases(suite)]
    began=time.monotonic()
    result=unittest.TextTestRunner(stream=stream,verbosity=2,resultclass=Results).run(suite)
    counts=dict(testsRun=result.testsRun,failures=len(result.failures),errors=len(result.errors),
        skipped=len(result.skipped),expected_failures=len(result.expectedFailures),unexpected_successes=len(result.unexpectedSuccesses))
    return dict(scope='whole_project',configured=configured,discovered_ids=ids,started_ids=result.started_ids,
        terminal_results=result.terminal,modules=modules,loader_errors=list(loader_errors),**counts,
        duration_seconds=time.monotonic()-began,exit_code=int(any(v for k,v in counts.items() if k!='testsRun') or bool(loader_errors)))


def configured_environment(run_root):
    original=Path('C:/Users/hp/Documents/外专双百')
    paths={'OILWELL_INGEST_TEST_CONFIG':'data/ingest-reliability-test/ingest-test.local.toml',
        'OILWELL_INGEST_PROVISION_IDENTITY':'data/registry-test/identity.json',
        'OILWELL_REGISTRY_TEST_CONFIG':'data/registry-test/registry-test.local.toml',
        'OILWELL_POSTGRES_TEST_CONFIG':'data/registry-test/legacy-test.local.toml',
        'OILWELL_WAVEFORM_TEST_CONFIG':'data/waveform-test/waveform-test.local.toml',
        'OILWELL_WAVEFORM_PROVISION_IDENTITY':'data/registry-test/identity.json'}
    for value in paths.values():
        if not (original/value).is_file(): raise ValueError('Required isolated fixture is absent')
    for path in ('C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe','C:/Program Files/mosquitto/mosquitto.exe'):
        if not Path(path).is_file(): raise ValueError('Required verification dependency is absent')
    return dict(os.environ,**{key:str(original/value) for key,value in paths.items()},PYTHONUTF8='1',NO_BROWSER='1',
        OILWELL_TEST_UNUSED_MQTT='not-connected-test-value',PLATFORM_DATABASE_PATH=str(run_root/'platform.db'),
        PLATFORM_LOG_PATH=str(run_root/'runtime-logs'),OILWELL_INGEST_EVIDENCE_DIR=str(run_root/'browser-evidence'),
        OILWELL_WAVEFORM_EVIDENCE_DIR=str(run_root/'waveform-evidence'))


def regression_phase(root):
    os.environ.update(configured_environment(root))
    os.chdir(ROOT)
    loader=unittest.TestLoader()
    suite=loader.discover('tests',pattern='test_*.py')
    modules=sorted(str(p.relative_to(ROOT/'tests')).replace('\\','/') for p in (ROOT/'tests').rglob('test_*.py'))
    with (root/'full-regression.log').open('x',encoding='utf-8') as output:
        result=run_suite(suite,output,modules=modules,configured=True,loader_errors=loader.errors)
    result.update(run_id=root.name[-32:],source_sha256=inventory_sha256(source_inventory()))
    (root/'regression.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    return result['exit_code']


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',required=True); parser.add_argument('--evidence-dir',required=True)
    parser.add_argument('--duration-seconds',type=int,default=1800)
    parser.add_argument('--include-existing-regression',action='store_true')
    parser.add_argument('--continue-from',type=Path)
    parser.add_argument('--continue-sha256')
    parser.add_argument('--continue-source-sha256')
    parser.add_argument('--continue-resources-from',type=Path)
    parser.add_argument('--continue-resources-sha256')
    parser.add_argument('--continue-recovery-from',type=Path)
    parser.add_argument('--continue-recovery-sha256')
    parser.add_argument('--phase',choices=('all','regression'),default='all',help=argparse.SUPPRESS)
    args=parser.parse_args(argv)
    continuation_args=(args.continue_from,args.continue_sha256,args.continue_source_sha256)
    resource_args=(args.continue_resources_from,args.continue_resources_sha256)
    retained_args=(args.continue_recovery_from,args.continue_recovery_sha256)
    if any(retained_args) and (not all(retained_args) or not all(resource_args) or not all(continuation_args) or
        args.phase!='all' or not args.include_existing_regression): raise ValueError('Complete retained binding and inclusive gate required')
    if any(resource_args) and (not all(resource_args) or not all(continuation_args) or args.phase!='all' or not args.include_existing_regression):
        raise ValueError('Complete resources binding and inclusive regression required')
    if any(continuation_args) and (not all(continuation_args) or args.phase!='all'):
        raise ValueError('Complete explicit continuation binding required')
    before=source_inventory() if args.phase=='all' else None
    retained=None
    if args.continue_recovery_from:
        retained=validate_retained_continuation(args.continue_recovery_from,args.continue_recovery_sha256,args.continue_source_sha256,before,
            original_path=args.continue_from,original_sha256=args.continue_sha256,
            resources_path=args.continue_resources_from,resources_sha256=args.continue_resources_sha256)
        binding=retained['original']; resources=retained['resources']
    else:
        binding=validate_continuation(args.continue_from,args.continue_sha256,args.continue_source_sha256,before,
            recovery=bool(args.continue_resources_from)) if args.continue_from else None
        resources=validate_resources_continuation(args.continue_resources_from,args.continue_resources_sha256,binding,before) if args.continue_resources_from else None
    scope='retained_target_and_regression' if retained else 'recovery_and_regression' if resources else 'suffix_and_regression' if binding else 'whole_run'
    from tools.waveform_restore import load_source_authority, create_run_root
    authority=load_source_authority(args.config,resolve=False)
    root=Path(args.evidence_dir)
    if args.phase=='regression':
        # Parent has created the exclusive run manifest; never accept arbitrary output.
        from tools.waveform_restore import validate_run_root
        validate_run_root(root,existing=True)
        return regression_phase(root)
    root,run_id=create_run_root(root)
    report=dict(schema='waveform.verification/1',run_id=run_id,evidence_kind='actual',status='partial')
    began=time.monotonic()
    from tools.verify_ingest_reliability import protected_snapshot
    protection=protected_snapshot()
    try:
        from tests.waveform_experiments import run_experiments,run_retained_restore
        prefix=copy_continuation(binding,root) if binding else None
        resources_prefix=copy_resources_continuation(resources,root) if resources else None
        if retained:
            from tools.waveform_restore import RETAINED_RUN_ID,RETAINED_REPORT_SHA256,RETAINED_SOURCE_SHA256
            copy_retained_continuation(retained,root,run_id)
            report.update(evidence_mode='four_segment',segments=[resources['prior_segment'],resources['segment'],
                dict(kind='reused_source_preparation_and_lease',run_id=RETAINED_RUN_ID,report_sha256=RETAINED_REPORT_SHA256,
                    report_ref='prior-r90/report.json',files_sha256=retained['bundle']['report']['source']['files_sha256'],
                    source_sha256=RETAINED_SOURCE_SHA256,historical_status='partial',validated=True,
                    excluded_target='oilwell_waveforms_restore_'+RETAINED_RUN_ID,excluded_reason='safety.host_reserve'),
                dict(kind='fresh_target_and_regression',run_id=run_id,source_sha256=inventory_sha256(before),
                    approved_source_sha256=args.continue_source_sha256,source_transition=retained['transition'])])
        elif resources:
            report.update(evidence_mode='three_segment',segments=[resources['prior_segment'],resources['segment'],
                dict(kind='fresh_recovery_and_regression',run_id=run_id,source_sha256=inventory_sha256(before),approved_source_sha256=args.continue_source_sha256,
                    **({'source_transition':resources['segment']['source_transition']} if resources['segment'].get('source_transition') else {}),
                    **({'schema_version':5} if resources['segment'].get('recovery_exception') else {}))])
        elif binding:
            old=binding['report']
            report.update(evidence_mode='two_segment',segments=[dict(kind='reused_protocol_soak',run_id=old['run_id'],validated=True,
                report_sha256=binding['report_sha256'],report_ref='prior-run1/report.json',files_sha256=old['source']['files_sha256'],
                source_sha256=inventory_sha256(old['source']['files_sha256']),source_delta=binding['source_delta'],
                **({'product_exception':binding['product_exception']} if binding.get('product_exception') is not None else {})),
                dict(kind='fresh_suffix_and_regression',run_id=run_id,source_sha256=inventory_sha256(before),approved_source_sha256=args.continue_source_sha256)])
        if retained:
            report.update(run_retained_restore(root,run_id,args.config,prefix=prefix,resources_prefix=resources_prefix,source_sha256=inventory_sha256(before)))
        else:
            report.update(run_experiments(root,run_id,args.config,args.duration_seconds,prefix=prefix,resources_prefix=resources_prefix,source_sha256=inventory_sha256(before)))
        report['source']=dict(files_sha256=before,unchanged=source_inventory()==before,scope=scope)
        report['protection']=dict(unchanged=protected_snapshot()==protection)
        # Do not burn the inclusive gate when required experiments are already
        # incomplete. Only test results and final status remain to be supplied.
        prerequisites=[reason for reason in qualify(report) if not reason.startswith('tests.') and reason!='report.incomplete']
        prerequisites+=verify_lease_artifact(root,report)
        prerequisites+=verify_recovery_artifact(root,report)
        if prerequisites: raise RuntimeError('Experiments cannot qualify for final regression')
        if args.include_existing_regression:
            command=[sys.executable,str(Path(__file__).resolve()),'--config',args.config,'--evidence-dir',str(root),'--phase','regression']
            result=subprocess.run(command,cwd=ROOT,env=configured_environment(root),check=False)
            report['tests']=json.loads((root/'regression.json').read_text(encoding='utf-8'))
            if result.returncode!=report['tests']['exit_code']: raise ValueError('Regression process status mismatch')
        report['status']='completed'
    except BaseException as error:
        # Exception text may contain credentials/private payloads. Preserve type only.
        if (root/'experiment-partial.json').is_file():
            report.update(json.loads((root/'experiment-partial.json').read_text(encoding='utf-8')))
        report['error_type']=type(error).__name__
        import traceback
        report['error_frames']=[dict(file=frame.filename,line=frame.lineno,function=frame.name) for frame in traceback.extract_tb(error.__traceback__)]
    finally:
        report['source']={'files_sha256':before,'unchanged':source_inventory()==before,'scope':scope}
        report['protection']={'before':protection,'after':protected_snapshot()}
        report['protection']['unchanged']=report['protection']['before']==report['protection']['after']
        report['total_elapsed_seconds']=time.monotonic()-began
        report['artifacts']=[dict(relative_path=str(p.relative_to(root)).replace('\\','/'),size=p.stat().st_size,sha256=digest_file(p))
            for p in sorted(root.rglob('*')) if p.is_file() and p!=root/'report.json']
        report['qualification_reasons']=list(qualify(report))
        report['qualification_reasons']+=verify_lease_artifact(root,report)
        report['qualification_reasons']+=verify_recovery_artifact(root,report)
        if report.get('artifacts'): report['qualification_reasons']+=verify_artifacts(root,report['artifacts'])
        (root/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps({'run_id':run_id,'qualified':not report['qualification_reasons'],'reasons':report['qualification_reasons']}))
    return int(bool(report['qualification_reasons']))


if __name__=='__main__': raise SystemExit(main())
