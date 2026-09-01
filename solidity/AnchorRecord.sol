// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

/// @title AnchorRecord — minimal integrity-anchoring contract (CONTRACTS.md §4)
/// @notice Anchors provenance fingerprints of pipeline results on Polygon Amoy.
///     What this proves, stated precisely: that a specific fingerprint
///     (content_hash) was anchored immutably at a given time and has not
///     changed since. It does NOT prove who is in any photo — that claim is
///     made by the off-chain verification step, not by this chain.
/// @dev On-chain data is minimized per CONTRACTS.md §4: no embeddings, no raw
///     source URLs, no image bytes, no submitter PII. Only hashes, an optional
///     IPFS CID, the verification decision label, and provenance metadata.
contract AnchorRecord {
    struct Record {
        bytes32 contentHash;            // sha256 of the canonical record JSON
        string contentCid;              // IPFS CID of the pinned record ("" if pinning failed)
        bytes32 sourceReferenceHash;    // sha256(candidate_url) — never the raw URL
        string verificationResult;      // "candidate_match" | "uncertain" | "no_match"
        uint256 anchoredAt;             // block timestamp at anchoring
        address anchoredBy;             // wallet that submitted the anchor tx
    }

    mapping(bytes32 => Record) private _records;

    event RecordAnchored(
        bytes32 indexed recordId,
        bytes32 indexed contentHash,
        string contentCid,
        uint256 anchoredAt
    );

    /// @notice Anchor (or re-anchor with an updated record under a new id) a
    ///     pipeline result. recordId is a uuid4 rendered as bytes32; anchoring
    ///     the same recordId twice reverts so an anchored record cannot be
    ///     silently overwritten.
    function anchorRecord(
        bytes32 recordId,
        bytes32 contentHash,
        string calldata contentCid,
        bytes32 sourceReferenceHash,
        string calldata verificationResult
    ) external {
        require(_records[recordId].anchoredAt == 0, "record already anchored");
        require(contentHash != bytes32(0), "empty content hash");

        _records[recordId] = Record({
            contentHash: contentHash,
            contentCid: contentCid,
            sourceReferenceHash: sourceReferenceHash,
            verificationResult: verificationResult,
            anchoredAt: block.timestamp,
            anchoredBy: msg.sender
        });

        emit RecordAnchored(recordId, contentHash, contentCid, block.timestamp);
    }

    /// @notice Read back a full anchored record for independent re-verification.
    function getRecord(bytes32 recordId) external view returns (Record memory) {
        return _records[recordId];
    }

    /// @notice Tamper-evidence check: does the presented content hash still
    ///     match what was anchored? Rebuild the hash off-chain (including after
    ///     a pixel edit in the demo) and compare here.
    function isIntact(bytes32 recordId, bytes32 contentHash) external view returns (bool) {
        return _records[recordId].contentHash == contentHash;
    }
}
