"""stage 3 quizzes + word_occurrences (Q5 数据飞轮)

新增两张派生/缓存表：
- quizzes：从 Explanation 标注派生的测验题
- word_occurrences：词 → 内容反向索引（真实语境例句）

Revision ID: c3f8a4e1d2b6
Revises: b7e2a1d9c4f3
Create Date: 2026-05-18

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c3f8a4e1d2b6'
down_revision: Union[str, Sequence[str], None] = 'b7e2a1d9c4f3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'quizzes',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('content_id', sa.String(length=64), nullable=False),
        sa.Column('segment_id', sa.Integer(), nullable=True),
        sa.Column('kind', sa.String(length=32), nullable=False),
        sa.Column('question', sa.String(length=2048), nullable=False),
        sa.Column('options', sa.JSON(), nullable=False),
        sa.Column('answer_index', sa.Integer(), nullable=False),
        sa.Column('rationale', sa.String(length=2048), nullable=False),
        sa.Column('source_model', sa.String(length=64), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['content_id'], ['contents.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('quizzes', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_quizzes_content_id'), ['content_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_quizzes_kind'), ['kind'], unique=False)

    op.create_table(
        'word_occurrences',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('word', sa.String(length=64), nullable=False),
        sa.Column('content_id', sa.String(length=64), nullable=False),
        sa.Column('segment_id', sa.Integer(), nullable=False),
        sa.Column('sentence', sa.String(length=2048), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['content_id'], ['contents.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('word_occurrences', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_word_occurrences_word'), ['word'], unique=False)
        batch_op.create_index(batch_op.f('ix_word_occurrences_content_id'), ['content_id'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('word_occurrences', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_word_occurrences_content_id'))
        batch_op.drop_index(batch_op.f('ix_word_occurrences_word'))
    op.drop_table('word_occurrences')
    with op.batch_alter_table('quizzes', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_quizzes_kind'))
        batch_op.drop_index(batch_op.f('ix_quizzes_content_id'))
    op.drop_table('quizzes')
