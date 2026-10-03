"""正确性测试：多证据、替代路径、最终拆分、历史版本不可变、重开库。"""

import pytest

from identity_history import (
    AddEvidence,
    IdentityHistoryStore,
    QueryItem,
    RevokeEvidence,
)


@pytest.fixture()
def db_path(tmp_path):
    return str(tmp_path / "identity.db")


def make_store(db_path, visitors, customers):
    return IdentityHistoryStore.create(db_path, visitors, customers)


def group(store, visitor, version):
    return store.query([QueryItem(visitor, version)])[0]


class TestMultipleEvidenceSameRelation:
    """同一关系存在多条证据：撤销其中一条不影响其他证据。"""

    def test_duplicate_relation_survives_single_revoke(self, db_path):
        s = make_store(db_path, ["v1", "v2"], [])
        s.append_changes(
            [
                AddEvidence("e1", visitor_a="v1", visitor_b="v2"),  # v1
                AddEvidence("e2", visitor_a="v1", visitor_b="v2"),  # v2
                RevokeEvidence("e1"),  # v3
            ]
        )
        assert group(s, "v1", 3).visitors == ("v1", "v2")
        assert group(s, "v2", 3).visitors == ("v1", "v2")

    def test_relation_splits_after_all_evidence_revoked(self, db_path):
        s = make_store(db_path, ["v1", "v2"], [])
        s.append_changes(
            [
                AddEvidence("e1", visitor_a="v1", visitor_b="v2"),
                AddEvidence("e2", visitor_a="v1", visitor_b="v2"),
                RevokeEvidence("e1"),
                RevokeEvidence("e2"),  # v4：最后一条撤销
            ]
        )
        assert group(s, "v1", 4).visitors == ("v1",)
        assert group(s, "v2", 4).visitors == ("v2",)
        # 历史版本不受影响
        assert group(s, "v1", 2).visitors == ("v1", "v2")


class TestAlternativePaths:
    """组内存在替代路径：撤销冗余路径上的证据不能拆组。"""

    def test_triangle_survives_edge_revoke(self, db_path):
        s = make_store(db_path, ["v1", "v2", "v3"], [])
        s.append_changes(
            [
                AddEvidence("e1", visitor_a="v1", visitor_b="v2"),
                AddEvidence("e2", visitor_a="v2", visitor_b="v3"),
                AddEvidence("e3", visitor_a="v1", visitor_b="v3"),
                RevokeEvidence("e1"),  # v4：v1-v2 仍有 v1-v3-v2 路径
            ]
        )
        for v in ("v1", "v2", "v3"):
            assert group(s, v, 4).visitors == ("v1", "v2", "v3")

    def test_group_splits_when_last_bridge_revoked(self, db_path):
        s = make_store(db_path, ["a1", "a2", "b1", "b2"], [])
        s.append_changes(
            [
                AddEvidence("e1", visitor_a="a1", visitor_b="a2"),
                AddEvidence("e2", visitor_a="b1", visitor_b="b2"),
                AddEvidence("e3", visitor_a="a2", visitor_b="b1"),  # 桥
                RevokeEvidence("e3"),  # v4：拆成 {a1,a2} 与 {b1,b2}
            ]
        )
        assert group(s, "a1", 4).visitors == ("a1", "a2")
        assert group(s, "b2", 4).visitors == ("b1", "b2")
        # 撤销前是一整组
        assert group(s, "a1", 3).visitors == ("a1", "a2", "b1", "b2")


class TestFullHistory:
    """小规模历史：逐版本与独立核定的预期结果完全一致。"""

    def test_every_version_matches_expected(self, db_path):
        visitors = ["v1", "v2", "v3", "v4"]
        customers = ["c1"]
        s = make_store(db_path, visitors, customers)
        s.append_changes(
            [
                AddEvidence("e1", visitor_a="v1", visitor_b="v2"),      # v1
                AddEvidence("e2", visitor_a="v2", visitor_b="v3"),      # v2
                AddEvidence("e3", visitor_a="v3", customer_id="c1"),    # v3
                RevokeEvidence("e2"),                                   # v4
                AddEvidence("e4", visitor_a="v1", visitor_b="v4"),      # v5
                RevokeEvidence("e1"),                                   # v6
            ]
        )
        # 独立核定的预期：{visitor: (各版本组成员, 客户)}
        expected = {
            "v1": [
                (("v1",), None),                      # v0
                (("v1", "v2"), None),                 # v1
                (("v1", "v2", "v3"), None),           # v2
                (("v1", "v2", "v3"), "c1"),           # v3
                (("v1", "v2"), None),                 # v4
                (("v1", "v2", "v4"), None),           # v5
                (("v1", "v4"), None),                 # v6
            ],
            "v3": [
                (("v3",), None),
                (("v3",), None),
                (("v1", "v2", "v3"), None),
                (("v1", "v2", "v3"), "c1"),
                (("v3",), "c1"),
                (("v3",), "c1"),
                (("v3",), "c1"),
            ],
            "v4": [
                (("v4",), None),
                (("v4",), None),
                (("v4",), None),
                (("v4",), None),
                (("v4",), None),
                (("v1", "v2", "v4"), None),
                (("v1", "v4"), None),
            ],
        }
        items = [
            QueryItem(visitor, version)
            for visitor, rows in expected.items()
            for version in range(len(rows))
        ]
        results = s.query(items)
        i = 0
        for visitor, rows in expected.items():
            for version, (exp_visitors, exp_customer) in enumerate(rows):
                got = results[i]
                i += 1
                assert got.visitors == exp_visitors, (visitor, version, got)
                assert got.customer_id == exp_customer, (visitor, version, got)
                assert got.identified == (exp_customer is not None)

    def test_intermediate_versions_within_one_batch(self, db_path):
        """同批追加的中间版本必须可查询。"""
        s = make_store(db_path, ["v1", "v2", "v3"], [])
        s.append_changes(
            [
                AddEvidence("e1", visitor_a="v1", visitor_b="v2"),  # v1
                AddEvidence("e2", visitor_a="v2", visitor_b="v3"),  # v2
                RevokeEvidence("e1"),                               # v3
            ]
        )
        assert group(s, "v1", 1).visitors == ("v1", "v2")
        assert group(s, "v1", 2).visitors == ("v1", "v2", "v3")
        assert group(s, "v1", 3).visitors == ("v1",)
        assert group(s, "v3", 3).visitors == ("v2", "v3")

    def test_version_numbers_are_sequential(self, db_path):
        s = make_store(db_path, ["v1", "v2"], [])
        assert s.append_changes([AddEvidence("e1", visitor_a="v1", visitor_b="v2")]) == [1]
        assert s.append_changes(
            [AddEvidence("e2", visitor_a="v1", visitor_b="v2"), RevokeEvidence("e1")]
        ) == [2, 3]
        assert s.current_version == 3


class TestHistoryImmutability:
    """后续变更不能改变更早版本的查询结果。"""

    def test_old_versions_unchanged_after_more_appends(self, db_path):
        s = make_store(db_path, ["v1", "v2", "v3"], ["c1"])
        s.append_changes([AddEvidence("e1", visitor_a="v1", visitor_b="v2")])
        before = s.query([QueryItem("v1", v) for v in range(2)])
        s.append_changes(
            [
                AddEvidence("e2", visitor_a="v2", visitor_b="v3"),
                AddEvidence("e3", visitor_a="v3", customer_id="c1"),
                RevokeEvidence("e1"),
            ]
        )
        after = s.query([QueryItem("v1", v) for v in range(2)])
        assert before == after


class TestReopen:
    """正常关闭并重新打开库后，所有版本仍可查询。"""

    def test_reopen_preserves_all_versions(self, db_path):
        s = make_store(db_path, ["v1", "v2", "v3"], ["c1"])
        s.append_changes(
            [
                AddEvidence("e1", visitor_a="v1", visitor_b="v2"),
                AddEvidence("e2", visitor_a="v2", customer_id="c1"),
                RevokeEvidence("e1"),
                AddEvidence("e3", visitor_a="v2", visitor_b="v3"),
            ]
        )
        items = [QueryItem(v, ver) for v in ("v1", "v2", "v3") for ver in range(5)]
        expected = s.query(items)
        s.close()

        s2 = IdentityHistoryStore.open(db_path)
        assert s2.current_version == 4
        assert s2.query(items) == expected
        # 重开后还能继续追加，版本号连续
        assert s2.append_changes([RevokeEvidence("e2")]) == [5]
        assert group(s2, "v2", 5).customer_id is None
        assert group(s2, "v2", 4).customer_id == "c1"
        s2.close()


class TestValidation:
    def test_unknown_visitor_rejected(self, db_path):
        s = make_store(db_path, ["v1"], [])
        with pytest.raises(ValueError):
            s.append_changes([AddEvidence("e1", visitor_a="v1", visitor_b="nope")])
        with pytest.raises(ValueError):
            s.query([QueryItem("nope", 0)])

    def test_duplicate_evidence_id_rejected(self, db_path):
        s = make_store(db_path, ["v1", "v2"], [])
        s.append_changes([AddEvidence("e1", visitor_a="v1", visitor_b="v2")])
        with pytest.raises(ValueError):
            s.append_changes([AddEvidence("e1", visitor_a="v1", visitor_b="v2")])

    def test_double_revoke_rejected(self, db_path):
        s = make_store(db_path, ["v1", "v2"], [])
        s.append_changes(
            [AddEvidence("e1", visitor_a="v1", visitor_b="v2"), RevokeEvidence("e1")]
        )
        with pytest.raises(ValueError):
            s.append_changes([RevokeEvidence("e1")])

    def test_future_version_rejected(self, db_path):
        s = make_store(db_path, ["v1"], [])
        with pytest.raises(ValueError):
            s.query([QueryItem("v1", 1)])

    def test_overlapping_ids_rejected(self, tmp_path):
        with pytest.raises(ValueError):
            IdentityHistoryStore.create(
                str(tmp_path / "x.db"), ["v1", "shared"], ["shared"]
            )
