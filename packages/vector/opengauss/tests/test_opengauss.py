import pytest
from unittest.mock import AsyncMock, MagicMock, patch
import asyncio

from cognee_community_vector_adapter_opengauss import OpenGaussAdapter


class TestOpenGaussAdapter:
    """Unit test suite for openGauss DataVec adapter"""
    
    @pytest.fixture
    def mock_embedding_engine(self):
        """Create mock embedding engine"""
        engine = AsyncMock()
        engine.embed_text = AsyncMock(return_value=[[0.1] * 1536])
        engine.get_vector_size = MagicMock(return_value=1536)
        return engine
    
    @pytest.fixture
    def adapter(self, mock_embedding_engine):
        """Create adapter instance with mocked connection"""
        with patch.object(OpenGaussAdapter, '_get_connection') as mock_conn:
            mock_cursor = MagicMock()
            mock_conn.return_value = MagicMock()
            
            adapter = OpenGaussAdapter(
                url="postgresql://gaussdb:test@localhost:5432/testdb",
                api_key="test_password",
                embedding_engine=mock_embedding_engine,
                database_name="test_db",
                index_type="HNSW",
                distance_strategy="COSINE",
                embedding_dimension=1536,
            )
            
            # Mock cursor
            adapter._get_cursor = MagicMock(return_value=mock_cursor)
            
            yield adapter
    
    @pytest.mark.asyncio
    async def test_init_basic(self, mock_embedding_engine):
        """Test basic initialization"""
        adapter = OpenGaussAdapter(
            url="postgresql://user:pass@localhost:5432/db",
            api_key=None,
            embedding_engine=mock_embedding_engine,
        )
        
        assert adapter.name == "openGauss"
        assert adapter.url == "postgresql://user:pass@localhost:5432/db"
        assert adapter.database_name == "cognee"
        assert adapter.index_type == "HNSW"
        assert adapter.distance_strategy == "COSINE"
    
    @pytest.mark.asyncio
    async def test_init_custom_config(self, mock_embedding_engine):
        """Test initialization with custom configuration"""
        adapter = OpenGaussAdapter(
            url="postgresql://test@test:5432/test",
            api_key="test",
            embedding_engine=mock_embedding_engine,
            database_name="custom_db",
            index_type="IVFFLAT",
            distance_strategy="EUCLIDEAN",
            embedding_dimension=768,
        )
        
        assert adapter.database_name == "custom_db"
        assert adapter.index_type == "IVFFLAT"
        assert adapter.distance_strategy == "EUCLIDEAN"
        assert adapter.embedding_dimension == 768
    
    def test_validate_config_valid(self, mock_embedding_engine):
        """Test configuration validation with valid parameters"""
        adapter = OpenGaussAdapter(
            url="postgresql://test@localhost:5432/test",
            api_key="test",
            embedding_engine=mock_embedding_engine,
            index_type="HNSW",
            distance_strategy="COSINE",
        )
        # Should not raise any exception
        assert True
    
    def test_validate_config_invalid_distance(self, mock_embedding_engine):
        """Test configuration validation with invalid distance strategy"""
        with pytest.raises(ValueError, match="Invalid distance strategy"):
            OpenGaussAdapter(
                url="postgresql://test@localhost:5432/test",
                api_key="test",
                embedding_engine=mock_embedding_engine,
                distance_strategy="INVALID_DISTANCE"
            )
    
    @pytest.mark.asyncio
    async def test_embed_data(self, adapter):
        """Test text embedding functionality"""
        data = ["Hello world", "Test text"]
        result = await adapter.embed_data(data)
        
        # Verify result is a list of vectors
        assert isinstance(result, list)
        assert len(result) > 0
        # Verify each vector has correct dimension
        if len(result) > 0:
            assert all(len(vec) == 1536 for vec in result)
    
    @pytest.mark.asyncio
    async def test_has_collection_exists(self, adapter):
        """Test collection existence check - exists case"""
        mock_cursor = adapter._get_cursor.return_value
        mock_cursor.fetchone.return_value = {'exists': True}
        
        result = await adapter.has_collection("test_collection")
        
        assert result is True
        mock_cursor.execute.assert_called_once()
    
    @pytest.mark.asyncio
    async def test_has_collection_not_exists(self, adapter):
        """Test collection existence check - not exists case"""
        mock_cursor = adapter._get_cursor.return_value
        mock_cursor.fetchone.return_value = {'exists': False}
        
        result = await adapter.has_collection("nonexistent")
        
        assert result is False
    
    @pytest.mark.asyncio
    async def test_create_collection_success(self, adapter):
        """Test successful collection creation"""
        # Mock has_collection to return False
        with patch.object(adapter, 'has_collection', return_value=False):
            mock_cursor = adapter._get_cursor.return_value
            mock_conn = adapter._get_connection()
            
            await adapter.create_collection("new_collection")
            
            # Verify CREATE TABLE was called
            assert mock_cursor.execute.called
            mock_conn.commit.assert_called_once()
    
    @pytest.mark.asyncio
    async def test_create_collection_already_exists(self, adapter):
        """Test collection creation - skip when already exists"""
        with patch.object(adapter, 'has_collection', return_value=True):
            mock_cursor = adapter._get_cursor.return_value
            
            await adapter.create_collection("existing_collection")
            
            # Should not execute CREATE TABLE
            mock_cursor.execute.assert_not_called()
    
    @pytest.mark.asyncio
    async def test_create_data_points_empty(self, adapter):
        """Test creating empty data points list"""
        result = await adapter.create_data_points("test_table", [])
        # Should return normally without error
        assert result is None
    
    @pytest.mark.asyncio
    async def test_search_with_query_text(self, adapter):
        """Test search using query text"""
        from uuid import uuid4
        query_vector = [0.1] * 1536
        test_id = str(uuid4())
        
        with patch.object(adapter, 'has_collection', return_value=True), \
             patch.object(adapter, 'embed_data', return_value=[query_vector]):
            
            mock_row = {"id": test_id, "score": 0.95, "text": "Test document"}
            mock_cursor = adapter._get_cursor.return_value
            mock_cursor.__iter__ = MagicMock(return_value=iter([mock_row]))

            results = await adapter.search(
                collection_name="test_table",
                query_text="test query",
                limit=5
            )

            assert len(results) == 1
            assert str(results[0].id) == test_id
            assert abs(results[0].score - 0.95) < 0.001
    
    @pytest.mark.asyncio
    async def test_search_with_query_vector(self, adapter):
        """Test search using pre-computed query vector"""
        query_vector = [0.2] * 1536
        
        with patch.object(adapter, 'has_collection', return_value=True):
            mock_cursor = adapter._get_cursor.return_value
            mock_cursor.fetchall.return_value = []
            
            results = await adapter.search(
                collection_name="test_table",
                query_vector=query_vector,
                limit=10
            )
            
            # Should NOT call embed_data when vector is provided directly
            adapter.embedding_engine.embed_text.assert_not_called()
    
    @pytest.mark.asyncio
    async def test_search_missing_parameters(self, adapter):
        """Test search with missing required parameters raises error"""
        with patch.object(adapter, 'has_collection', return_value=True):
            from cognee.infrastructure.databases.exceptions import MissingQueryParameterError
            
            with pytest.raises(MissingQueryParameterError):
                await adapter.search(
                    collection_name="test_table",
                    query_text=None,
                    query_vector=None
                )
    
    @pytest.mark.asyncio
    async def test_search_collection_not_exists(self, adapter):
        """Test search returns empty results for non-existent collection"""
        with patch.object(adapter, 'has_collection', return_value=False):
            results = await adapter.search(
                collection_name="nonexistent",
                query_text="test"
            )
            
            assert results == []
    
    @pytest.mark.asyncio
    async def test_batch_search(self, adapter):
        """Test batch search functionality"""
        query_vectors = [[0.1] * 1536, [0.2] * 1536]
        
        with patch.object(adapter, 'embed_data', return_value=query_vectors), \
             patch.object(adapter, 'search', return_value=[]) as mock_search:
            
            results = await adapter.batch_search(
                collection_name="test_table",
                query_texts=["query1", "query2"],
                limit=5
            )
            
            assert mock_search.call_count == 2
            assert len(results) == 2
    
    @pytest.mark.asyncio
    async def test_retrieve_by_ids(self, adapter):
        """Test retrieving data points by IDs"""
        mock_cursor = adapter._get_cursor.return_value
        mock_cursor.fetchall.return_value = [
            {'id': '1', 'text': 'Doc 1', 'metadata': '{}'},
            {'id': '2', 'text': 'Doc 2', 'metadata': '{}'}
        ]
        
        results = await adapter.retrieve(
            collection_name="test_table",
            data_point_ids=["1", "2"]
        )
        
        assert len(results) == 2
        assert results[0]['id'] == '1'
    
    @pytest.mark.asyncio
    async def test_retrieve_empty_ids(self, adapter):
        """Test retrieve with empty ID list returns empty"""
        results = await adapter.retrieve(
            collection_name="test_table",
            data_point_ids=[]
        )
        
        assert results == []
    
    @pytest.mark.asyncio
    async def test_delete_data_points(self, adapter):
        """Test deleting data points"""
        mock_cursor = adapter._get_cursor.return_value
        mock_cursor.rowcount = 3
        mock_conn = adapter._get_connection()
        
        result = await adapter.delete_data_points(
            collection_name="test_table",
            data_point_ids=["1", "2", "3"]
        )
        
        assert result["deleted_count"] == 3
        mock_conn.commit.assert_called_once()
    
    @pytest.mark.asyncio
    async def test_delete_data_points_empty(self, adapter):
        """Test delete with empty ID list returns zero count"""
        result = await adapter.delete_data_points(
            collection_name="test_table",
            data_point_ids=[]
        )
        
        assert result["deleted_count"] == 0
    
    @pytest.mark.asyncio
    async def test_create_vector_index_skipped_by_default(self, adapter):
        """create_vector_index creates table but skips index when flag is off."""
        with patch.object(adapter, "has_collection", return_value=False):
            mock_cursor = adapter._get_cursor.return_value
            mock_conn = adapter._get_connection()

            await adapter.create_vector_index("test_table", "vector")

            create_index_calls = [
                c[0][0] for c in mock_cursor.execute.call_args_list
                if "CREATE INDEX" in str(c[0][0])
            ]
            assert len(create_index_calls) == 0

    @pytest.mark.asyncio
    async def test_create_vector_index_when_enabled(self, adapter):
        """create_vector_index creates index when flag is on."""
        adapter.create_index = True
        with patch.object(adapter, "has_collection", return_value=False):
            mock_cursor = adapter._get_cursor.return_value
            mock_conn = adapter._get_connection()

            await adapter.create_vector_index("test_table", "vector")

            create_index_calls = [
                c[0][0] for c in mock_cursor.execute.call_args_list
                if "CREATE INDEX" in str(c[0][0])
            ]
            assert len(create_index_calls) == 1
            assert "hnsw" in create_index_calls[0].lower()
    
    @pytest.mark.asyncio
    async def test_prune(self, adapter):
        """Test pruning/cleanup of all data"""
        mock_cursor = adapter._get_cursor.return_value
        mock_cursor.fetchall.return_value = [
            {'table_name': 'cognee_vectors'},
            {'table_name': 'cognee_test'}
        ]
        mock_conn = adapter._get_connection()
        
        await adapter.prune()
        
        # Should have at least SELECT + DROP TABLEs calls
        assert mock_cursor.execute.call_count >= 2
        mock_conn.commit.assert_called_once()
    
    @pytest.mark.asyncio
    async def test_health_check_healthy(self, adapter):
        """Test health check - healthy status"""
        mock_cursor = adapter._get_cursor.return_value
        mock_cursor.fetchone.return_value = {
            'version': 'openGauss 7.0.0 (DataVec)'
        }
        
        health = await adapter.health_check()
        
        assert health["status"] == "healthy"
        assert "database_version" in health
        assert health["adapter_name"] == "openGauss"
    
    @pytest.mark.asyncio
    async def test_health_check_unhealthy(self, adapter):
        """Test health check - unhealthy status due to connection error"""
        adapter._get_cursor.side_effect = Exception("Connection failed")
        
        health = await adapter.health_check()
        
        assert health["status"] == "unhealthy"
        assert "error" in health
    
    def test_get_collection_names(self, adapter):
        """Test getting list of collection names"""
        mock_cursor = adapter._get_cursor.return_value
        mock_cursor.fetchall.return_value = [
            {'table_name': 'table1'},
            {'table_name': 'table2'}
        ]
        
        names = adapter.get_collection_names()
        
        assert names == ['table1', 'table2']
    
    @pytest.mark.asyncio
    async def test_close_connection(self, adapter):
        """Test closing database connection properly"""
        mock_conn = MagicMock()
        mock_conn.closed = 0
        adapter._connection = mock_conn

        await adapter.close()

        mock_conn.close.assert_called_once()
        assert adapter._connection is None


class TestOpenGaussAdapterEdgeCases:
    """Edge case and exception handling tests"""
    
    @pytest.fixture
    def adapter(self):
        """Create minimal adapter instance for edge case testing"""
        engine = AsyncMock()
        engine.embed_text = AsyncMock(return_value=[[0.1] * 1536])
        
        return OpenGaussAdapter(
            url="postgresql://test@localhost:5432/test",
            api_key="test",
            embedding_engine=engine,
        )
    
    @pytest.mark.asyncio
    async def test_large_batch_insert(self, adapter):
        """Test performance with large batch insert"""
        from cognee.infrastructure.engine import DataPoint
        from uuid import uuid4
        
        large_batch = [
            DataPoint(
                id=uuid4(),
                text=f"Document {i} content",
                metadata={"type": "document", "index_fields": ["text"]}
            )
            for i in range(1000)
        ]
        
        with patch.object(adapter, '_get_cursor'), \
             patch.object(adapter, '_get_connection'), \
             patch.object(adapter, 'embed_data', return_value=[[0.1] * 1536] * 1000) as mock_embed:
            
            await adapter.create_data_points("test_table", large_batch)
            
            # Verify embed was called once (batch processing)
            mock_embed.assert_called_once()
    
    @pytest.mark.asyncio
    async def test_concurrent_operations(self, adapter):
        """Test concurrent operation safety"""
        with patch.object(adapter, 'has_collection', return_value=True), \
             patch.object(adapter, 'search', return_value=[]):
            
            tasks = [
                adapter.search("test_table", query_text=f"query_{i}")
                for i in range(10)
            ]
            
            results = await asyncio.gather(*tasks)
            
            assert len(results) == 10


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
