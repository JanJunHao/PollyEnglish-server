"""unified content model (Q2)

contents 升级为统一内容基表：新增 kind / language / topics / 质量分 / attribution，
视频专属列 duration_seconds / play_mode 放开为可空（图文行为 NULL）；
新增 article_details 形态明细表。

Revision ID: b7e2a1d9c4f3
Revises: 00386555715c
Create Date: 2026-05-18

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b7e2a1d9c4f3'
down_revision: Union[str, Sequence[str], None] = '00386555715c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # contents：新增 Q2 共享元数据列 + 放开视频专属列为可空。
    # server_default 让已有 69 行视频在 ADD COLUMN 时自动获得默认值。
    with op.batch_alter_table('contents', schema=None) as batch_op:
        batch_op.add_column(sa.Column('kind', sa.String(length=16),
                                      nullable=False, server_default='video'))
        batch_op.add_column(sa.Column('language', sa.String(length=16),
                                      nullable=False, server_default='en'))
        batch_op.add_column(sa.Column('topics', sa.JSON(),
                                      nullable=False, server_default=sa.text("'[]'")))
        batch_op.add_column(sa.Column('audio_teachability', sa.Float(), nullable=True))
        batch_op.add_column(sa.Column('quality_score', sa.Float(), nullable=True))
        batch_op.add_column(sa.Column('attribution', sa.String(length=1024), nullable=True))
        # 视频专属列放开为可空——图文（kind='article'）行这两列为 NULL
        batch_op.alter_column('duration_seconds',
                              existing_type=sa.Integer(), nullable=True)
        batch_op.alter_column('play_mode',
                              existing_type=sa.String(length=16), nullable=True)
        batch_op.create_index(batch_op.f('ix_contents_kind'), ['kind'], unique=False)

    # article_details：图文形态明细表，与 contents 基表 1:1
    op.create_table(
        'article_details',
        sa.Column('content_id', sa.String(length=64), nullable=False),
        sa.Column('body', sa.Text(), nullable=False),
        sa.Column('paragraphs', sa.JSON(), nullable=False),
        sa.Column('image_urls', sa.JSON(), nullable=False),
        sa.Column('word_count', sa.Integer(), nullable=False),
        sa.Column('reading_time_seconds', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['content_id'], ['contents.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('content_id'),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('article_details')
    with op.batch_alter_table('contents', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_contents_kind'))
        batch_op.alter_column('play_mode',
                              existing_type=sa.String(length=16), nullable=False)
        batch_op.alter_column('duration_seconds',
                              existing_type=sa.Integer(), nullable=False)
        batch_op.drop_column('attribution')
        batch_op.drop_column('quality_score')
        batch_op.drop_column('audio_teachability')
        batch_op.drop_column('topics')
        batch_op.drop_column('language')
        batch_op.drop_column('kind')
